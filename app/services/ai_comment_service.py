import html
import logging
import re

from flask import current_app

try:
    import requests
except ImportError:  # The normal application must start without AI extras.
    requests = None


LOGGER = logging.getLogger(__name__)
SKIP_COMMENT = 'SKIP_COMMENT'

SYSTEM_PROMPT = """你是娱乐社区中的AI评论机器人“评论特伯罗”。

你的任务是根据用户发布的帖子，生成一条自然、简短、友善的中文评论。

你的人设：
- 像一个懂梗但不过度玩梗的网友。
- 偶尔使用幽默、捧哏、反差和轻度吐槽。
- 对作品分享给予具体鼓励。
- 对压力、失败和低落内容给予温和回应。
- 不强行搞笑，不说教，不冒充真人。

回复要求：
1. 只输出一条语义完整的评论正文，通常15到60个汉字。
2. 不重复帖子原文，不使用模板化开头。
3. 不攻击、羞辱或引导危险行为。
4. 不提供医疗、法律、金融等专业结论。
5. 不询问隐私，不生成广告，不提及API、模型或提示词。
6. 评论必须以句号、问号、感叹号或省略号结束，不能说到一半。
7. 对严肃内容保持克制；不合适时只返回：SKIP_COMMENT。"""


class AICommentError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class AICommentService:
    """Rule-first, OpenAI-compatible AI comment generation."""

    SKIP_PATTERNS = (
        r'自杀|自残|轻生|不想活', r'癌症|重病|急救|诊断|用药',
        r'死亡|去世|灾难|事故|遇难', r'身份证|银行卡|手机号|家庭住址',
        r'未成年人.{0,8}(姓名|学校|住址)', r'仇恨|人肉|网暴|威胁',
        r'选举|政治|法律咨询|律师|投资建议|股票推荐|贷款',
        r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b',
        r'(?<!\d)1[3-9]\d{9}(?!\d)',
        r'(?<!\d)\d{17}[\dXx](?!\d)',
    )
    SUPPORTIVE_PATTERNS = (
        r'压力|焦虑|难过|低落|失落|崩溃|疲惫|失败|没考好|加班|孤独',
    )
    ENCOURAGING_PATTERNS = (
        r'作品|画了|拍了|摄影|创作|练习|学习|完成|做了|成果|翻唱|剪辑',
    )
    HUMOROUS_PATTERNS = (r'吐槽|笑死|离谱|搞笑|社死|日常|今天|哈哈',)

    def should_comment(self, post):
        text = '{}\n{}'.format(post.title or '', post.content or '').strip()
        compact = re.sub(r'\s+', '', text)
        if len(compact) < 8 or re.fullmatch(r'[\W_\d]+', compact):
            return {'should_comment': False, 'tone': 'skip',
                    'reason': '正文过短或无有效内容'}
        if re.fullmatch(r'https?://\S+', text, re.IGNORECASE):
            return {'should_comment': False, 'tone': 'skip',
                    'reason': '仅包含链接'}
        for pattern in self.SKIP_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return {'should_comment': False, 'tone': 'skip',
                        'reason': '内容需要谨慎处理'}
        for patterns, tone, reason in (
                (self.SUPPORTIVE_PATTERNS, 'supportive', '需要温和陪伴'),
                (self.ENCOURAGING_PATTERNS, 'encouraging', '作品或成果分享'),
                (self.HUMOROUS_PATTERNS, 'humorous', '普通生活分享')):
            if any(re.search(pattern, text, re.IGNORECASE)
                   for pattern in patterns):
                return {'should_comment': True, 'tone': tone,
                        'reason': reason}
        return {'should_comment': True, 'tone': 'neutral',
                'reason': '普通分享'}

    def generate_post_comment(self, post, tone=None):
        decision = self.should_comment(post)
        if not decision['should_comment']:
            return None
        tone = tone or decision['tone']
        prompt = (
            '帖子标题：{title}\n帖子正文：{content}\n帖子分类：{category}\n'
            '帖子标签：{tags}\n推荐语气：{tone}'
        ).format(title=(post.title or '')[:200],
                 content=(post.content or '')[:1500],
                 category=post.category or '',
                 tags='、'.join(post.tags or []), tone=tone)
        last_error = None
        for attempt in range(2):
            retry_prompt = prompt
            if attempt:
                retry_prompt += ('\n上一条候选回复不完整，请重新写一条完整、简短的评论，'
                                 '不要续写上一条。')
            try:
                text = self._request(retry_prompt)
                return self.validate_generated_comment(text)
            except AICommentError as error:
                last_error = error
                if error.code not in ('incomplete_sentence',
                                      'truncated_response'):
                    raise
                LOGGER.info('Retrying incomplete AI comment for post %s',
                            getattr(post, 'id', None))
        raise last_error

    def generate_reply(self, post, user_comment):
        raise AICommentError('reply_not_supported', '当前评论系统暂不支持楼中楼回复。')

    def validate_generated_comment(self, text):
        if not isinstance(text, str):
            raise AICommentError('invalid_response', 'AI返回格式无效。')
        cleaned = html.unescape(text).strip().strip('`').strip()
        cleaned = re.sub(r'<[^>]*>', '', cleaned)
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip(' "“”')
        if not cleaned:
            raise AICommentError('empty_response', 'AI未返回评论。')
        if cleaned.upper() == SKIP_COMMENT:
            return None
        if len(cleaned) > 80:
            raise AICommentError('response_too_long', 'AI评论超过长度限制。')
        if re.search(r'<script|javascript:|onerror\s*=', cleaned,
                     re.IGNORECASE):
            raise AICommentError('unsafe_response', 'AI评论未通过安全检查。')
        if not re.search(r'[。！？!?…~～][”’"』」）)]?$', cleaned):
            raise AICommentError('incomplete_sentence',
                                 'AI评论不是完整句子。')
        return cleaned

    def _request(self, user_prompt):
        api_key = current_app.config.get('AI_API_KEY', '')
        if not api_key:
            raise AICommentError('not_configured', 'AI服务尚未配置。')
        if requests is None:
            raise AICommentError('dependency_missing', 'AI请求客户端未安装。')
        base_url = current_app.config.get('AI_BASE_URL', '').rstrip('/')
        model = current_app.config.get('AI_MODEL', '')
        if not base_url or not model:
            raise AICommentError('not_configured', 'AI服务配置不完整。')
        try:
            response = requests.post(
                base_url + '/chat/completions',
                headers={'Authorization': 'Bearer ' + api_key,
                         'Content-Type': 'application/json'},
                json={'model': model,
                      'messages': [
                          {'role': 'system', 'content': SYSTEM_PROMPT},
                          {'role': 'user', 'content': user_prompt}],
                      'temperature': 0.75, 'max_tokens': 256},
                timeout=current_app.config.get('AI_REQUEST_TIMEOUT', 12))
            response.raise_for_status()
            payload = response.json()
            choice = payload['choices'][0]
            if choice.get('finish_reason') == 'length':
                raise AICommentError('truncated_response',
                                     'AI评论生成被截断。')
            return choice['message']['content']
        except requests.Timeout as error:
            LOGGER.warning('AI comment request timed out')
            raise AICommentError('timeout', 'AI服务请求超时。') from error
        except (requests.RequestException, KeyError, IndexError,
                TypeError, ValueError) as error:
            LOGGER.warning('AI comment request failed: %s',
                           type(error).__name__)
            raise AICommentError('api_error', 'AI服务暂时不可用。') from error
