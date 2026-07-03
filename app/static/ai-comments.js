(function() {
    function addBotComment(section, data) {
        if (!data.comment || document.getElementById('comment-' + data.comment.id)) return;
        var comment = data.comment;
        var item = document.createElement('div');
        item.className = 'comment-item ai-comment-item';
        item.id = 'comment-' + comment.id;

        var header = document.createElement('div');
        header.className = 'comment-header';
        var avatarBox = document.createElement('div');
        avatarBox.className = 'comment-avatar-container';
        var avatar = document.createElement('img');
        avatar.className = 'comment-avatar';
        avatar.src = comment.avatar_url;
        avatar.alt = comment.nickname;
        avatarBox.appendChild(avatar);

        var meta = document.createElement('div');
        meta.className = 'comment-meta';
        var nameLine = document.createElement('div');
        var author = document.createElement('a');
        author.className = 'comment-author-link comment-nickname';
        author.href = comment.profile_url;
        author.textContent = comment.nickname;
        var badge = document.createElement('span');
        badge.className = 'ai-bot-badge';
        badge.textContent = 'AI机器人';
        badge.title = '内容由AI生成';
        var time = document.createElement('span');
        time.className = 'comment-time';
        time.textContent = comment.created_at;
        nameLine.appendChild(author);
        nameLine.appendChild(badge);
        meta.appendChild(nameLine);
        meta.appendChild(time);

        var body = document.createElement('div');
        body.className = 'comment-body';
        body.textContent = comment.content;
        header.appendChild(avatarBox);
        header.appendChild(meta);
        item.appendChild(header);
        item.appendChild(body);
        section.querySelector('#commentList').appendChild(item);
        var counter = section.querySelector('#commentCount');
        if (counter && data.count !== undefined) {
            counter.dataset.count = data.count;
            counter.textContent = '评论 (' + data.count + ')';
        }
    }

    function initializeAIComment() {
        var section = document.querySelector('.comment-section[data-ai-generate-url]');
        if (!section || section.dataset.aiRequested === 'true') return;
        section.dataset.aiRequested = 'true';
        var status = section.querySelector('.ai-comment-status');
        var csrf = document.querySelector('meta[name="csrf-token"]');
        fetch(section.dataset.aiGenerateUrl, {
            method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json',
                      'X-CSRFToken': csrf ? csrf.content : ''},
            body: '{}'
        }).then(function(response) {
            return response.json().then(function(payload) {
                if (!response.ok && response.status !== 409) {
                    throw new Error(payload.error || 'AI评论暂时没有出现。');
                }
                return payload;
            });
        }).then(function(data) {
            if (data.status === 'generated') addBotComment(section, data);
            if (status) status.textContent = '';
            section.removeAttribute('data-ai-generate-url');
        }).catch(function(error) {
            if (status) status.textContent = error.message || 'AI评论暂时没有出现。';
            section.removeAttribute('data-ai-generate-url');
        });
    }

    document.addEventListener('rx:page-ready', initializeAIComment);
    document.addEventListener('click', function(event) {
        var button = event.target.closest('.js-ai-admin-retry');
        if (!button || button.disabled) return;
        var status = document.querySelector('.ai-admin-status');
        var csrf = document.querySelector('meta[name="csrf-token"]');
        button.disabled = true;
        if (status) status.textContent = '正在重新生成…';
        fetch(button.dataset.aiRetryUrl, {
            method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json',
                      'X-CSRFToken': csrf ? csrf.content : ''},
            body: JSON.stringify({force: true})
        }).then(function(response) {
            return response.json().then(function(payload) {
                if (!response.ok) throw new Error(payload.error || '重新生成失败。');
                return payload;
            });
        }).then(function(data) {
            if (status) status.textContent = data.status === 'generated' ?
                'AI评论已生成。刷新后可查看最新状态。' : '本次未生成评论。';
            button.remove();
        }).catch(function(error) {
            if (status) status.textContent = error.message;
            button.disabled = false;
        });
    });
    if (document.readyState !== 'loading') initializeAIComment();
    else document.addEventListener('DOMContentLoaded', initializeAIComment);
})();
