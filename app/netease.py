import re


class NeteaseMusicError(RuntimeError):
    pass


HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36'),
    'Referer': 'https://music.163.com/'
}


def _request_json(url, timeout):
    try:
        import certifi
        import requests
    except ImportError as error:
        raise NeteaseMusicError(
            '缺少 requests/certifi 依赖，请先安装 requirements.txt。') from error
    try:
        response = requests.get(url, headers=HEADERS, timeout=timeout,
                                verify=certifi.where())
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as error:
        raise NeteaseMusicError('网易云音乐查询失败，请稍后重试。') from error


def query_netease_song(song_id, timeout=6):
    song_id = str(song_id or '').strip()
    if not re.fullmatch(r'\d{1,20}', song_id):
        raise NeteaseMusicError('请输入有效的网易云歌曲 ID。')
    detail_url = (
        'https://music.163.com/api/song/detail/?id={0}&ids=[{0}]'
        .format(song_id))
    payload = _request_json(detail_url, timeout)

    songs = payload.get('songs') or []
    if not songs:
        raise NeteaseMusicError('未找到该歌曲，可能是 ID 错误或歌曲不可用。')
    song = songs[0]
    artists = song.get('artists') or []
    album = song.get('album') or {}
    return {
        'id': song_id,
        'title': song.get('name') or '未知歌曲',
        'artist': artists[0].get('name') if artists else '未知歌手',
        'cover': album.get('picUrl') or '',
        'duration_ms': int(song.get('duration') or song.get('dt') or 0),
        'stream_url': ('https://music.163.com/song/media/outer/url'
                       '?id={}.mp3'.format(song_id))
    }
