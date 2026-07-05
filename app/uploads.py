from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from uuid import uuid4

from flask import current_app
from PIL import Image, UnidentifiedImageError

from app.extensions import db
from app.models import Media, PostImage


MAX_IMAGE_SIZE = 5 * 1024 * 1024
MAX_POST_IMAGES = 9
MAX_VIDEO_SIZE = 30 * 1024 * 1024
MAX_POST_VIDEOS = 1
FORMAT_RULES = {
    'JPEG': ({'.jpg', '.jpeg'}, '.jpg', 'image/jpeg'),
    'PNG': ({'.png'}, '.png', 'image/png'),
    'GIF': ({'.gif'}, '.gif', 'image/gif'),
    'WEBP': ({'.webp'}, '.webp', 'image/webp')
}


class UploadValidationError(ValueError):
    pass


@dataclass
class PreparedImage:
    data: bytes
    extension: str
    mime_type: str


@dataclass
class PreparedVideo:
    data: bytes
    extension: str
    mime_type: str


def _transcode_hevc_mp4(data):
    """Convert phone-recorded HEVC MP4 to a browser-friendly H.264 MP4."""
    try:
        import imageio_ffmpeg
        executable = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError) as error:
        current_app.logger.error('HEVC transcoder is unavailable: %s', error)
        raise UploadValidationError('服务器暂时无法转换该 HEVC/H.265 视频，请稍后重试。')

    with TemporaryDirectory() as directory:
        source = Path(directory) / 'source.mp4'
        output = Path(directory) / 'output.mp4'
        source.write_bytes(data)
        command = [
            executable, '-hide_banner', '-loglevel', 'error', '-y',
            '-i', str(source), '-map', '0:v:0', '-map', '0:a?',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
            '-vf', ('scale=1280:720:force_original_aspect_ratio=decrease:'
                    'force_divisible_by=2,fps=30'),
            '-profile:v', 'main', '-level:v', '3.1',
            '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k',
            '-movflags', '+faststart', str(output)
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, check=False,
                timeout=current_app.config.get('VIDEO_TRANSCODE_TIMEOUT', 180)
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            current_app.logger.warning('HEVC transcode failed: %s', error)
            raise UploadValidationError('视频转换超时或失败，请缩短视频后重试。')
        if completed.returncode != 0 or not output.exists():
            details = completed.stderr.decode('utf-8', errors='replace')[-1000:]
            current_app.logger.warning('HEVC transcode failed: %s', details)
            raise UploadValidationError('视频转换失败，请确认文件完整后重试。')
        converted = output.read_bytes()
        if not converted:
            raise UploadValidationError('视频转换失败，请确认文件完整后重试。')
        return converted


def _upload_root():
    configured = current_app.config.get('UPLOAD_FOLDER')
    if configured:
        return Path(configured).resolve()
    return (Path(current_app.static_folder) / 'uploads' / 'posts').resolve()


def prepare_image(file_storage):
    filename = (file_storage.filename or '').strip()
    if not filename:
        raise UploadValidationError('请选择图片文件。')
    original_extension = Path(filename).suffix.lower()
    allowed_extensions = {extension for extensions, _, _ in FORMAT_RULES.values()
                          for extension in extensions}
    if original_extension not in allowed_extensions:
        raise UploadValidationError('仅支持 jpg、jpeg、png、gif、webp 图片。')

    file_storage.stream.seek(0)
    data = file_storage.stream.read(MAX_IMAGE_SIZE + 1)
    file_storage.stream.seek(0)
    if len(data) > MAX_IMAGE_SIZE:
        raise UploadValidationError('单张图片不能超过 5MB。')
    if not data:
        raise UploadValidationError('图片文件不能为空。')

    try:
        with Image.open(BytesIO(data)) as image:
            image_format = (image.format or '').upper()
            image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise UploadValidationError('文件内容不是有效图片。')

    if image_format not in FORMAT_RULES:
        raise UploadValidationError('图片内容类型不受支持。')
    valid_extensions, canonical_extension, mime_type = FORMAT_RULES[image_format]
    if original_extension not in valid_extensions:
        raise UploadValidationError('图片扩展名与实际内容不一致。')
    return PreparedImage(data=data, extension=canonical_extension,
                         mime_type=mime_type)


def prepare_images(file_storages, existing_count=0):
    files = [item for item in file_storages if item and item.filename]
    if existing_count + len(files) > MAX_POST_IMAGES:
        raise UploadValidationError('每篇博文最多上传 9 张图片。')
    return [prepare_image(item) for item in files]


def store_images(post, uploader_id, prepared_images):
    root = _upload_root()
    root.mkdir(parents=True, exist_ok=True)
    created_paths = []
    start_order = len(post.image_items)
    try:
        for offset, prepared in enumerate(prepared_images):
            filename = '{}{}'.format(uuid4().hex, prepared.extension)
            absolute_path = root / filename
            absolute_path.write_bytes(prepared.data)
            relative_path = 'uploads/posts/{}'.format(filename)
            media = Media(uploader_id=uploader_id, media_type='image',
                          file_path=relative_path,
                          mime_type=prepared.mime_type,
                          file_size=len(prepared.data))
            post.image_items.append(PostImage(media=media,
                                               sort_order=start_order + offset))
            created_paths.append(absolute_path)
        return created_paths
    except Exception:
        cleanup_paths(created_paths)
        raise


def prepare_video(file_storage):
    filename = (file_storage.filename or '').strip()
    if not filename:
        raise UploadValidationError('请选择视频文件。')
    extension = Path(filename).suffix.lower()
    if extension not in {'.mp4', '.webm'}:
        raise UploadValidationError('仅支持 MP4、WebM 视频。')

    file_storage.stream.seek(0)
    data = file_storage.stream.read(MAX_VIDEO_SIZE + 1)
    file_storage.stream.seek(0)
    if len(data) > MAX_VIDEO_SIZE:
        raise UploadValidationError('视频文件不能超过 30MB。')
    if not data:
        raise UploadValidationError('视频文件不能为空。')

    if extension == '.mp4':
        valid_content = len(data) >= 12 and data[4:8] == b'ftyp'
        mime_type = 'video/mp4'
    else:
        valid_content = data.startswith(b'\x1a\x45\xdf\xa3')
        mime_type = 'video/webm'
    if not valid_content:
        raise UploadValidationError('文件内容不是有效的 {} 视频。'.format(
            'MP4' if extension == '.mp4' else 'WebM'))
    # MP4 is only a container. Phones commonly record HEVC/H.265 inside it;
    # transparently convert those videos so browsers receive H.264 instead.
    if extension == '.mp4' and (b'hvc1' in data or b'hev1' in data):
        data = _transcode_hevc_mp4(data)
    return PreparedVideo(data=data, extension=extension, mime_type=mime_type)


def prepare_videos(file_storages, existing_count=0):
    files = [item for item in file_storages if item and item.filename]
    if existing_count + len(files) > MAX_POST_VIDEOS:
        raise UploadValidationError('每篇博文最多上传 1 个视频。')
    return [prepare_video(item) for item in files]


def store_videos(post, uploader_id, prepared_videos):
    root = _upload_root()
    root.mkdir(parents=True, exist_ok=True)
    created_paths = []
    start_order = len(post.image_items)
    try:
        for offset, prepared in enumerate(prepared_videos):
            filename = '{}{}'.format(uuid4().hex, prepared.extension)
            absolute_path = root / filename
            absolute_path.write_bytes(prepared.data)
            relative_path = 'uploads/posts/{}'.format(filename)
            media = Media(uploader_id=uploader_id, media_type='video',
                          file_path=relative_path,
                          mime_type=prepared.mime_type,
                          file_size=len(prepared.data))
            post.image_items.append(PostImage(media=media,
                                               sort_order=start_order + offset))
            created_paths.append(absolute_path)
        return created_paths
    except Exception:
        cleanup_paths(created_paths)
        raise


def cleanup_paths(paths):
    root = _upload_root()
    for path in paths:
        candidate = Path(path).resolve()
        if candidate.parent == root and candidate.exists():
            try:
                candidate.unlink()
            except OSError:
                current_app.logger.warning('无法清理上传文件：%s', candidate)


def media_absolute_path(media):
    filename = Path(media.file_path).name
    return _upload_root() / filename


@dataclass
class PreparedSingleFile:
    data: bytes
    extension: str
    mime_type: str
    media_type: str


ALLOWED_AUDIO_EXTENSIONS = {'.mp3', '.ogg', '.wav'}
ALLOWED_AUDIO_MIMES = {
    'audio/mpeg': '.mp3',
    'audio/ogg': '.ogg',
    'audio/wav': '.wav',
    'audio/x-wav': '.wav',
}
MAX_AUDIO_SIZE = 10 * 1024 * 1024


def prepare_single_file(file_storage, file_type='image'):
    filename = (file_storage.filename or '').strip()
    if not filename:
        return None

    original_extension = Path(filename).suffix.lower()

    if file_type == 'audio':
        if original_extension not in ALLOWED_AUDIO_EXTENSIONS:
            raise UploadValidationError('仅支持 mp3、ogg、wav 音频文件。')

        file_storage.stream.seek(0)
        data = file_storage.stream.read(MAX_AUDIO_SIZE + 1)
        file_storage.stream.seek(0)

        if len(data) > MAX_AUDIO_SIZE:
            raise UploadValidationError('音频文件不能超过 10MB。')
        if not data:
            raise UploadValidationError('音频文件不能为空。')

        mime_type = ALLOWED_AUDIO_MIMES.get('audio/mpeg', '.mp3')
        if original_extension == '.ogg':
            mime_type = 'audio/ogg'
        elif original_extension == '.wav':
            mime_type = 'audio/wav'

        return PreparedSingleFile(data=data, extension=original_extension,
                                  mime_type=mime_type, media_type='audio')
    else:
        if original_extension not in {ext for exts, _, _ in FORMAT_RULES.values()
                                       for ext in exts}:
            raise UploadValidationError('仅支持 jpg、png、gif、webp 图片。')

        file_storage.stream.seek(0)
        data = file_storage.stream.read(MAX_IMAGE_SIZE + 1)
        file_storage.stream.seek(0)

        if len(data) > MAX_IMAGE_SIZE:
            raise UploadValidationError('图片不能超过 5MB。')
        if not data:
            raise UploadValidationError('图片文件不能为空。')

        try:
            with Image.open(BytesIO(data)) as image:
                image_format = (image.format or '').upper()
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise UploadValidationError('文件内容不是有效图片。')

        if image_format not in FORMAT_RULES:
            raise UploadValidationError('图片内容类型不受支持。')

        valid_extensions, canonical_extension, mime_type = FORMAT_RULES[image_format]
        if original_extension not in valid_extensions:
            raise UploadValidationError('图片扩展名与实际内容不一致。')

        return PreparedSingleFile(data=data, extension=canonical_extension,
                                  mime_type=mime_type, media_type='image')


def store_single_file(uploader_id, prepared, subfolder='avatar'):
    root = _upload_root().parent / subfolder
    root.mkdir(parents=True, exist_ok=True)

    filename = '{}{}'.format(uuid4().hex, prepared.extension)
    absolute_path = root / filename
    absolute_path.write_bytes(prepared.data)

    relative_path = 'uploads/{}/{}'.format(subfolder, filename)

    media = Media(
        uploader_id=uploader_id,
        media_type=prepared.media_type,
        file_path=relative_path,
        mime_type=prepared.mime_type,
        file_size=len(prepared.data)
    )
    db.session.add(media)
    db.session.flush()

    return relative_path


def single_file_absolute_path(relative_path):
    """Resolve an uploaded profile/music path without escaping static/uploads."""
    if not relative_path:
        return None
    normalized = str(relative_path).replace('\\', '/').lstrip('/')
    if normalized.startswith('uploads/'):
        normalized = normalized[len('uploads/'):]
    upload_base = _upload_root().parent.resolve()
    candidate = (upload_base / normalized).resolve()
    if upload_base != candidate and upload_base not in candidate.parents:
        raise UploadValidationError('上传文件路径无效。')
    return candidate
