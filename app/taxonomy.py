"""Stable entertainment taxonomy shared by forms, views and migrations."""

CATEGORIES = (
    ('game', '游戏', '✦', '游戏体验、攻略与通关记录'),
    ('live', 'Live', '♪', '演唱会、线下活动与现场感想'),
    ('vtuber', 'Vtuber', '◈', '直播、歌回与虚拟主播相关内容'),
    ('photo', '光影相册', '▧', '摄影、旅行与生活影像'),
    ('daily', '日常杂谈', '☁', '生活碎片、随想与轻松闲聊'),
    ('study', '学习角', '◇', '偶尔出现的学习与项目笔记'),
)

CATEGORY_CHOICES = [(slug, label) for slug, label, _, _ in CATEGORIES]
CATEGORY_META = {
    slug: {'slug': slug, 'label': label, 'icon': icon, 'description': description}
    for slug, label, icon, description in CATEGORIES
}
DEFAULT_CATEGORY = 'daily'

TAG_ALIASES = {
    '原神': '游戏',
    '独立游戏': '游戏',
    '现场摄影': '摄影',
}


def normalize_tag(tag):
    value = (tag or '').strip()
    return TAG_ALIASES.get(value, value)


def normalize_tags(tags, limit=10):
    normalized = []
    for tag in tags or []:
        value = normalize_tag(tag)
        if value and value not in normalized:
            normalized.append(value)
    return normalized[:limit]


def infer_category(tags):
    values = {str(tag).strip().lower() for tag in tags or []}
    if values & {'vtuber', '虚拟主播', '歌回'}:
        return 'vtuber'
    if values & {'live', '演唱会', '演唱会repo', '线下活动'}:
        return 'live'
    if values & {'摄影', '现场摄影', '照片', '相册', '旅行', '随拍'}:
        return 'photo'
    if values & {'游戏', '原神', '独立游戏', '音游'}:
        return 'game'
    if values & {'学习', '项目', '笔记'}:
        return 'study'
    return DEFAULT_CATEGORY


def category_meta(slug):
    return CATEGORY_META.get(slug, CATEGORY_META[DEFAULT_CATEGORY])
