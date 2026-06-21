const API_BASE = "http://localhost:8000";
const WS_BASE = "ws://localhost:8000";
let CURRENT_USER_ID = null;
let chatSocket = null;
let chatSettings = {
    historyCount: 50
};

// Context menu state
let selectedMessageData = null;

// Lazy loading observer for images
let lazyLoadObserver = null;

function initLazyLoading() {
    if (lazyLoadObserver) return;

    lazyLoadObserver = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                const element = entry.target;
                const src = element.dataset.src;
                const fileId = element.dataset.fileId;

                lazyLoadObserver.unobserve(element);

                if (src) {
                    // Has URL - load directly
                    const img = document.createElement('img');
                    img.className = 'msg-image';
                    img.onclick = () => viewImage(src);
                    img.onerror = () => {
                        if (fileId) {
                            handleImageError(img, fileId);
                        } else {
                            img.alt = '[图片加载失败]';
                        }
                    };
                    img.src = src;
                    element.replaceWith(img);
                } else if (fileId) {
                    // No URL - fetch via API
                    autoLoadImage(element.id, fileId);
                }
            }
        });
    }, { rootMargin: '100px' }); // Pre-load 100px before visible
}

// Observe all lazy images in container
function observeLazyImages(container) {
    if (!lazyLoadObserver) initLazyLoading();

    const lazyElements = container.querySelectorAll('.msg-image-lazy');
    lazyElements.forEach(el => lazyLoadObserver.observe(el));
}

// Render rich message content from OneBot message array
function renderMessageContent(msgArray, rawMessage) {
    if (!Array.isArray(msgArray)) {
        return escapeHtml(rawMessage || msgArray || "");
    }

    let html = "";
    msgArray.forEach(seg => {
        switch (seg.type) {
            case 'text':
                html += escapeHtml(seg.data?.text || "");
                break;
            case 'image':
                // Generate unique ID for this image
                const imgId = `img_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;

                // Try multiple URL sources - NapCat with enableLocalFile2Url should provide url field
                let imgUrl = seg.data?.url || "";
                const fileData = seg.data?.file || "";
                const fileId = seg.data?.file_id || fileData;

                // Check if it's a valid web URL or base64
                if (!imgUrl && fileData) {
                    if (fileData.startsWith('http') || fileData.startsWith('data:')) {
                        imgUrl = fileData;
                    } else if (fileData.startsWith('base64://')) {
                        imgUrl = `data:image/png;base64,${fileData.replace('base64://', '')}`;
                    }
                    // Local paths (C:\, /, etc) will be handled by placeholder
                }

                // Use proxy for QQ CDN URLs to bypass CORS
                let displayUrl = imgUrl;
                if (imgUrl && imgUrl.includes('multimedia.nt.qq.com.cn')) {
                    displayUrl = `${API_BASE}/api/image/proxy?url=${encodeURIComponent(imgUrl)}`;
                }

                if (displayUrl && (displayUrl.startsWith('http') || displayUrl.startsWith('data:'))) {
                    // Lazy load: use data-src instead of src, placeholder spinner
                    html += `<div id="${imgId}" class="msg-image-lazy" data-src="${displayUrl}" data-file-id="${escapeHtml(fileId)}">
                        <div class="loading-spinner"></div>
                    </div>`;
                } else if (fileId) {
                    // No direct URL, show placeholder that loads on demand
                    html += `<div id="${imgId}" class="msg-image-lazy" data-file-id="${escapeHtml(fileId)}">
                        <div class="loading-spinner"></div>
                    </div>`;
                } else {
                    html += `[图片]`;
                }
                break;
            case 'face':
                html += `<span class="msg-face">[表情${seg.data?.id || ''}]</span>`;
                break;
            case 'reply':
                const replyId = seg.data?.id || "";
                html += `<div class="msg-reply" data-reply-id="${replyId}">回复消息</div>`;
                break;
            case 'file':
                const fileName = seg.data?.name || "文件";
                const fileSize = formatFileSize(seg.data?.size || 0);
                const docFileId = seg.data?.id || seg.data?.file_id || "";
                html += `
                    <div class="msg-file">
                        <span class="msg-file-icon">📄</span>
                        <div class="msg-file-info">
                            <div class="msg-file-name">${escapeHtml(fileName)}</div>
                            <div class="msg-file-size">${fileSize}</div>
                        </div>
                        <button class="msg-file-download" onclick="downloadFile('${docFileId}', '${escapeHtml(fileName)}')">下载</button>
                    </div>`;
                break;
            case 'json':
                try {
                    const jsonData = JSON.parse(seg.data?.data || "{}");
                    const meta = jsonData.meta || {};
                    const detail = meta.detail_1 || meta.news || {};
                    const title = detail.title || jsonData.prompt || "链接";
                    const desc = detail.desc || "";
                    const preview = detail.preview || "";
                    const jumpUrl = detail.jumpUrl || detail.qqdocurl || "#";
                    html += `
                        <div class="msg-link-card" onclick="window.open('${jumpUrl}', '_blank')">
                            ${preview ? `<img class="msg-link-img" src="${preview}">` : ''}
                            <div class="msg-link-info">
                                <div class="msg-link-title">${escapeHtml(title)}</div>
                                <div class="msg-link-desc">${escapeHtml(desc)}</div>
                            </div>
                        </div>`;
                } catch (e) {
                    html += `[卡片消息]`;
                }
                break;
            case 'at':
                const atQQ = seg.data?.qq || "";
                html += `<span class="msg-at">@${seg.data?.name || atQQ}</span>`;
                break;
            case 'forward':
                // Forward message - show as expandable card
                const forwardId = seg.data?.id || "";
                const forwardContent = seg.data?.content || [];
                let previewText = "";
                let msgCount = 0;

                if (Array.isArray(forwardContent) && forwardContent.length > 0) {
                    // Extract preview from first few messages
                    msgCount = forwardContent.length;
                    forwardContent.slice(0, 2).forEach(node => {
                        const name = node.name || node.sender?.nickname || "未知";
                        const text = node.content?.[0]?.data?.text || "[消息]";
                        previewText += `${name}: ${text.substring(0, 20)}...\n`;
                    });
                } else {
                    previewText = "合并转发消息";
                    msgCount = 1;
                }

                html += `
                    <div class="msg-forward" onclick="expandForward('${forwardId}')">
                        <div class="msg-forward-title">群聊的聊天记录</div>
                        <div class="msg-forward-preview">${escapeHtml(previewText)}</div>
                        <div class="msg-forward-count">查看${msgCount}条转发消息</div>
                    </div>`;
                break;
            case 'video':
                // Video message - show thumbnail with play button
                const videoUrl = seg.data?.url || seg.data?.file || "";
                const videoThumb = seg.data?.thumb || seg.data?.cover || "";
                const videoFileId = seg.data?.file_id || seg.data?.file || "";

                if (videoUrl && (videoUrl.startsWith('http') || videoUrl.startsWith('data:'))) {
                    html += `
                        <div class="msg-video" onclick="playVideo('${videoUrl}')">
                            ${videoThumb ? `<img src="${videoThumb}" class="msg-video-thumb">` : '<div class="msg-video-placeholder"></div>'}
                            <div class="msg-video-play">▶</div>
                        </div>`;
                } else if (videoFileId) {
                    html += `
                        <div class="msg-video msg-video-pending" data-file-id="${videoFileId}" onclick="loadVideo(this, '${videoFileId}')">
                            <div class="msg-video-placeholder"></div>
                            <div class="msg-video-play">↓</div>
                            <div class="msg-video-label">点击加载视频</div>
                        </div>`;
                } else {
                    html += `[视频]`;
                }
                break;
            case 'record':
                // Voice message
                const recordUrl = seg.data?.url || seg.data?.file || "";
                const recordFileId = seg.data?.file_id || seg.data?.file || "";
                if (recordUrl) {
                    html += `
                        <div class="msg-record" onclick="playAudio('${recordUrl}')">
                            🎤 语音消息 <span class="msg-record-duration">${seg.data?.duration || ''}s</span>
                        </div>`;
                } else if (recordFileId) {
                    html += `<div class="msg-record" onclick="loadAudio(this, '${recordFileId}')">🎤 点击加载语音</div>`;
                } else {
                    html += `[语音]`;
                }
                break;
            default:
                html += `[${seg.type}]`;
        }
    });
    return html;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function formatFileSize(bytes) {
    if (!bytes || bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
}

// Smart timestamp formatting: shows date for older messages
function formatMessageTime(unixTime) {
    const date = new Date(unixTime * 1000);
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const yesterday = new Date(today.getTime() - 86400000);
    const msgDate = new Date(date.getFullYear(), date.getMonth(), date.getDate());

    const timeStr = date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });

    if (msgDate.getTime() === today.getTime()) {
        // Today: just show time
        return timeStr;
    } else if (msgDate.getTime() === yesterday.getTime()) {
        // Yesterday
        return `昨天 ${timeStr}`;
    } else if (now.getTime() - date.getTime() < 7 * 86400000) {
        // Within a week: show weekday
        const weekdays = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
        return `${weekdays[date.getDay()]} ${timeStr}`;
    } else if (date.getFullYear() === now.getFullYear()) {
        // Same year: show month-day
        return `${date.getMonth() + 1}月${date.getDate()}日 ${timeStr}`;
    } else {
        // Different year: full date
        return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 ${timeStr}`;
    }
}

function viewImage(url) {
    document.getElementById('image-viewer-img').src = url;
    document.getElementById('image-viewer-modal').style.display = 'flex';
}

function closeImageViewerOnBackdrop(event) {
    // Only close if clicking on the backdrop itself, not on the image or close button
    if (event.target.id === 'image-viewer-modal') {
        closeModal('image-viewer-modal');
    }
}

// Auto-load image when no direct URL available
async function autoLoadImage(elementId, fileId) {
    const element = document.getElementById(elementId);
    if (!element || !fileId) return;

    try {
        const res = await fetch(`${API_BASE}/api/image/get?file=${encodeURIComponent(fileId)}`);
        if (res.ok) {
            const data = await res.json();
            if (data.url) {
                // Use proxy for QQ CDN URLs  
                let imgUrl = data.url;
                if (imgUrl.includes('multimedia.nt.qq.com.cn')) {
                    imgUrl = `${API_BASE}/api/image/proxy?url=${encodeURIComponent(imgUrl)}`;
                }
                const img = document.createElement('img');
                img.className = 'msg-image';
                img.src = imgUrl;
                img.onclick = () => viewImage(imgUrl);
                img.onerror = () => { img.alt = '[图片加载失败]'; };
                element.replaceWith(img);
                return;
            }
        }
        // Failed - show placeholder
        element.innerHTML = '<span style="color:#999;font-size:0.85em;">[图片]</span>';
    } catch (e) {
        element.innerHTML = '<span style="color:#999;font-size:0.85em;">[图片]</span>';
    }
}

// Handle image load error by trying to fetch via API
async function handleImageError(imgElement, fileId) {
    if (!fileId) {
        imgElement.alt = '[图片加载失败]';
        return;
    }

    try {
        // Get fresh URL from NapCat (cached URLs expire)
        const res = await fetch(`${API_BASE}/api/image/get?file=${encodeURIComponent(fileId)}`);
        if (res.ok) {
            const data = await res.json();
            if (data.url) {
                // Use proxy for QQ CDN URLs
                let newUrl = data.url;
                if (newUrl.includes('multimedia.nt.qq.com.cn')) {
                    newUrl = `${API_BASE}/api/image/proxy?url=${encodeURIComponent(newUrl)}`;
                }
                if (newUrl !== imgElement.src) {
                    imgElement.onerror = null; // Prevent infinite loop
                    imgElement.src = newUrl;
                    return;
                }
            }
        }
    } catch (e) {
        console.error("Image refresh error:", e);
    }
    imgElement.alt = '[图片加载失败]';
}


async function loadImage(element, fileId) {
    if (!fileId) return;
    try {
        element.textContent = '加载中...';
        const res = await fetch(`${API_BASE}/api/image/get?file=${encodeURIComponent(fileId)}`);
        if (res.ok) {
            const data = await res.json();
            if (data.url) {
                const img = document.createElement('img');
                img.className = 'msg-image';
                img.src = data.url;
                img.onclick = () => viewImage(data.url);
                element.replaceWith(img);
                return;
            }
        }
        element.textContent = '[图片加载失败]';
    } catch (e) {
        element.textContent = '[图片加载失败]';
    }
}

// Expand forward message
async function expandForward(forwardId) {
    if (!forwardId) {
        alert("无法加载：转发消息ID不存在");
        return;
    }
    try {
        const res = await fetch(`${API_BASE}/api/forward/get?id=${encodeURIComponent(forwardId)}`);
        if (res.ok) {
            const data = await res.json();
            showForwardModal(data.messages || []);
        } else {
            alert("加载转发消息失败");
        }
    } catch (e) {
        alert("加载转发消息失败: " + e.message);
    }
}

function showForwardModal(messages) {
    let html = '<div class="forward-modal-content">';
    html += '<h3>转发的聊天记录</h3>';
    html += '<div class="forward-messages">';

    messages.forEach(msg => {
        const name = msg.sender?.nickname || msg.name || "未知";
        const content = renderMessageContent(msg.content || msg.message, msg.raw_message);
        const time = msg.time ? formatMessageTime(msg.time) : "";
        html += `
            <div class="forward-msg-item">
                <div class="forward-msg-name">${escapeHtml(name)}</div>
                <div class="forward-msg-content">${content}</div>
                <div class="forward-msg-time">${time}</div>
            </div>`;
    });

    html += '</div><button onclick="closeModal(\'forward-modal\')">关闭</button></div>';

    let modal = document.getElementById('forward-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'forward-modal';
        modal.className = 'modal';
        document.body.appendChild(modal);
    }
    modal.innerHTML = `<div class="modal-content">${html}</div>`;
    modal.style.display = 'block';
}

// Video playback
function playVideo(url) {
    let modal = document.getElementById('video-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'video-modal';
        modal.className = 'modal';
        modal.innerHTML = `
            <span class="close" onclick="closeVideoModal()">&times;</span>
            <video id="video-player" controls autoplay style="max-width:90%;max-height:90%;"></video>`;
        document.body.appendChild(modal);
        // Click outside video to close
        modal.addEventListener('click', (e) => {
            if (e.target === modal) {
                closeVideoModal();
            }
        });
    }
    document.getElementById('video-player').src = url;
    modal.style.display = 'flex';
    modal.style.alignItems = 'center';
    modal.style.justifyContent = 'center';
}

function closeVideoModal() {
    const modal = document.getElementById('video-modal');
    const video = document.getElementById('video-player');
    if (video) video.pause();
    if (modal) modal.style.display = 'none';
}

async function loadVideo(element, fileId) {
    if (!fileId) return;
    try {
        element.querySelector('.msg-video-label').textContent = '加载中...';
        const res = await fetch(`${API_BASE}/api/file/download/${fileId}`);
        if (res.ok) {
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            playVideo(url);
        } else {
            alert("视频加载失败");
        }
    } catch (e) {
        alert("视频加载失败: " + e.message);
    }
}

// Audio playback
let audioPlayer = null;

function playAudio(url) {
    if (audioPlayer) {
        audioPlayer.pause();
    }
    audioPlayer = new Audio(url);
    audioPlayer.play();
}

async function loadAudio(element, fileId) {
    if (!fileId) return;
    try {
        element.textContent = '🎤 加载中...';
        const res = await fetch(`${API_BASE}/api/file/download/${fileId}`);
        if (res.ok) {
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            playAudio(url);
            element.textContent = '🎤 正在播放';
        } else {
            element.textContent = '🎤 加载失败';
        }
    } catch (e) {
        element.textContent = '🎤 加载失败';
    }
}

function log(msg) {
    const consoleDiv = document.getElementById('console-output');
    const p = document.createElement('div');
    p.textContent = `> ${msg}`;
    consoleDiv.appendChild(p);
    consoleDiv.scrollTop = consoleDiv.scrollHeight;
}

// Connect to WebSocket for real-time messages
function connectChatSocket() {
    if (chatSocket && chatSocket.readyState === WebSocket.OPEN) return;

    chatSocket = new WebSocket(`${WS_BASE}/ws/chat`);

    chatSocket.onopen = () => {
        log("[WS] 实时消息连接已建立");
    };

    chatSocket.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            if (msg.type === "new_message") {
                handleNewMessage(msg.data);
            }
        } catch (e) {
            console.error("WS parse error:", e);
        }
    };

    chatSocket.onclose = () => {
        log("[WS] 连接已断开，5秒后重连...");
        setTimeout(connectChatSocket, 5000);
    };

    chatSocket.onerror = (err) => {
        console.error("WS error:", err);
    };
}

function handleNewMessage(payload) {
    // Only render if it's for the currently active chat
    const targetQQ = document.getElementById('target-qq').value;
    const isGroup = document.getElementById('is-group').checked;
    const groupId = document.getElementById('group-id').value;

    const senderId = String(payload.sender?.user_id || "");
    const fromGroup = payload.group_id ? String(payload.group_id) : null;

    // Check if message belongs to current conversation
    let shouldRender = false;
    if (isGroup && fromGroup === groupId) {
        shouldRender = true;
    } else if (!isGroup && (senderId === targetQQ || senderId === CURRENT_USER_ID)) {
        shouldRender = true;
    }

    if (!shouldRender) return;

    // Render the new message
    const chatContainer = document.getElementById('chat-messages');
    const isSelf = senderId === CURRENT_USER_ID;

    // Format timestamp
    const timestamp = payload.time ? formatMessageTime(payload.time) : "";

    const div = document.createElement('div');
    div.className = `message-wrapper ${isSelf ? 'sent' : 'received'}`;
    div.dataset.messageId = payload.message_id || "";
    div.dataset.senderId = senderId;
    div.dataset.time = payload.time || "";
    div.dataset.rawMessage = payload.raw_message || "";

    // Use rich content renderer
    const contentHtml = renderMessageContent(payload.message, payload.raw_message);

    const avatarUrl = senderId ? `http://q1.qlogo.cn/g?b=qq&nk=${senderId}&s=100` : "";
    const senderName = payload.sender?.nickname || senderId;

    // Avatar always present, CSS handles positioning via flex-direction
    div.innerHTML = `
        <div class="message-avatar" style="background-image:url('${avatarUrl}'); background-size:cover;" onclick="showProfileCard('${senderId}')"></div>
        <div class="message-content">
            <div class="message-sender-name">${isSelf ? '' : escapeHtml(senderName)}</div>
            <div class="message-bubble">${contentHtml}</div>
            ${timestamp ? `<div class="message-meta">${timestamp}</div>` : ''}
        </div>
    `;

    // Add context menu listener
    div.addEventListener('contextmenu', (e) => showContextMenu(e, div));

    chatContainer.appendChild(div);
    chatContainer.scrollTop = chatContainer.scrollHeight;

    // Lazy load any images in the new message
    observeLazyImages(div);

    log(`[新消息] ${senderName}: ${(payload.raw_message || '').substring(0, 20)}...`);
}

// Current login method selection
let selectedLoginMethod = 'qrcode';

function selectLoginMethod(method) {
    selectedLoginMethod = method;

    // Update tab appearance
    document.querySelectorAll('.login-tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.method === method);
    });

    // Show/hide panels
    document.getElementById('quick-login-panel').classList.toggle('active', method === 'quick');
    document.getElementById('qrcode-login-panel').classList.toggle('active', method === 'qrcode');
}

async function loadSavedAccounts() {
    try {
        const res = await fetch(`${API_BASE}/api/napcat/accounts`);
        const data = await res.json();

        const select = document.getElementById('quick-login-qq');
        select.innerHTML = '<option value="" disabled selected>选择已保存的QQ</option>';

        if (data.accounts && data.accounts.length > 0) {
            data.accounts.forEach(acc => {
                const option = document.createElement('option');
                option.value = acc.qq;
                option.textContent = acc.nickname ? `${acc.nickname} (${acc.qq})` : acc.qq;
                select.appendChild(option);
            });
            log(`已加载 ${data.accounts.length} 个已保存的QQ账号`);
        } else {
            select.innerHTML = '<option value="" disabled selected>暂无已保存的账号</option>';
            log("没有找到已保存的QQ账号，请先使用扫码登录");
        }
    } catch (e) {
        log(`加载账号列表失败: ${e}`);
    }
}

async function toggleNapCat() {
    const btn = document.getElementById('toggle-napcat-btn');
    if (btn.textContent.includes("启动")) {
        // Start
        log("正在启动 NapCat...");

        // Prepare login request
        const loginRequest = {
            login_type: selectedLoginMethod,
            qq: null,
            password: null
        };

        if (selectedLoginMethod === 'quick') {
            const qqSelect = document.getElementById('quick-login-qq');
            if (!qqSelect.value) {
                alert('请先选择要登录的QQ账号');
                return;
            }
            loginRequest.qq = qqSelect.value;
            log(`使用快速登录: QQ ${loginRequest.qq}`);
        } else {
            log("使用扫码登录");
        }

        try {
            const res = await fetch(`${API_BASE}/api/napcat/start`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(loginRequest)
            });
            const data = await res.json();
            log(`NapCat: ${data.status} (PID: ${data.pid || 'N/A'})`);

            if (data.login_type === 'quick' && data.qq) {
                log(`快速登录中: QQ ${data.qq}`);
            }

            btn.textContent = "退出登录 (NapCat)";

            // Start polling for status (QR modal only for qrcode login)
            startQRPoll(selectedLoginMethod === 'qrcode');

        } catch (e) {
            log(`Error: ${e}`);
        }
    } else {
        // Stop
        try {
            // Optimistically update UI
            btn.textContent = "停止中...";
            await fetch(`${API_BASE}/api/napcat/stop`, { method: "POST" });
            log("NapCat 已停止");
            btn.textContent = "启动 NapCat";

            // Clear status
            document.getElementById('napcat-status').classList.remove('online');
            document.getElementById('napcat-status').classList.add('offline');
            document.getElementById('napcat-status').textContent = "未启动";

            // Clear user info if present
            const info = document.getElementById('napcat-user-info');
            if (info) info.remove();

            // Clear Chat List / Inputs?
            // Optional: document.getElementById('chat-list-container').innerHTML = "";

        } catch (e) {
            log(`Stop Error: ${e}`);
            btn.textContent = "退出登录 (NapCat)"; // Revert if failed
        }
    }
}

// Load saved accounts on page load
document.addEventListener('DOMContentLoaded', () => {
    loadSavedAccounts();
});

let qrPollInterval = null;
function startQRPoll(showQRModal = true) {
    // Poll for QR code every 2 seconds
    if (qrPollInterval) clearInterval(qrPollInterval);

    // Only open QR modal for QR code login
    if (showQRModal) {
        document.getElementById('qr-modal').style.display = 'block';
        document.getElementById('qr-image').alt = "正在获取二维码...";
    }

    qrPollInterval = setInterval(async () => {
        try {
            const res = await fetch(`${API_BASE}/api/napcat/qrcode`);
            if (res.ok) {
                const blob = await res.blob();
                const url = URL.createObjectURL(blob);
                document.getElementById('qr-image').src = url;
                document.getElementById('qr-image').alt = "请扫码";

                // Auto-show QR modal if it's hidden (quick login fallback case)
                const qrModal = document.getElementById('qr-modal');
                if (qrModal.style.display !== 'block' && qrModal.style.display !== 'flex') {
                    qrModal.style.display = 'block';
                    log("快速登录失败，请扫码登录");
                }
            }
        } catch (e) {
            // console.log("QR wait...");
        }
    }, 2000);
}

async function checkNapCatStatus() {
    try {
        const res = await fetch(`${API_BASE}/api/napcat/status`);
        const data = await res.json();
        const indicator = document.getElementById('napcat-status');
        const btn = document.getElementById('toggle-napcat-btn');

        // State machine based on API response
        // Priority: bot_online > ws_connected > running

        if (!data.running) {
            // NapCat process not running
            indicator.textContent = "未启动";
            indicator.classList.remove('online');
            indicator.classList.add('offline');
            if (btn.textContent.includes("退出")) btn.textContent = "启动 NapCat";
            return;
        }

        // Process is running
        if (btn.textContent.includes("启动")) btn.textContent = "退出登录 (NapCat)";
        indicator.classList.remove('offline');
        indicator.classList.add('online');

        if (data.bot_online) {
            // Update Global ID
            if (data.login_info && data.login_info.user_id) {
                CURRENT_USER_ID = String(data.login_info.user_id);
            }

            // Sync UI if needed
            const userInfoEl = document.getElementById('napcat-user-info');
            // Check if we need to render user info
            const currentDisplayId = userInfoEl ? userInfoEl.getAttribute('data-uid') : null;

            if (CURRENT_USER_ID && currentDisplayId !== CURRENT_USER_ID) {
                closeModal('qr-modal');
                if (qrPollInterval) clearInterval(qrPollInterval);
                displayUserInfo(data.login_info);
                fetchContacts();
                connectChatSocket();  // Start real-time message WebSocket
                log("登录成功: " + (data.login_info.nickname || CURRENT_USER_ID));
            } else if (!userInfoEl) {
                // Fallback if no ID available yet but online
                // Try fetching manually if login_info was empty in status
                if (!CURRENT_USER_ID) {
                    fetchUserInfo();
                }
            }
            // indicator.textContent = "已登录"; // Don't overwrite if displayUserInfo set it? displayUserInfo sets innerHTML.
        } else if (data.ws_connected) {
            // WebSocket connected but not logged in yet
            indicator.textContent = "运行中 (等待扫码)";
        } else {
            // Process running but WS not connected yet
            indicator.textContent = "启动中 (等待连接)";
        }
    } catch (e) {
        // Network error or API down
        const indicator = document.getElementById('napcat-status');
        indicator.textContent = "API 错误";
        indicator.classList.remove('online');
        indicator.classList.add('offline');
    }
}

// Modals
function openSettings() {
    document.getElementById('settings-modal').style.display = 'block';
}

function closeModal(id) {
    document.getElementById(id).style.display = 'none';
    if (id === 'qr-modal' && qrPollInterval) clearInterval(qrPollInterval);
}

function saveSettings() {
    closeModal('settings-modal');
    log("分析设置已保存");
}

function openChatSettings() {
    document.getElementById('chat-settings-modal').style.display = 'block';
}

function saveChatSettings() {
    const count = parseInt(document.getElementById('chat-history-count').value) || 50;
    chatSettings.historyCount = count;
    closeModal('chat-settings-modal');
    log(`聊天设置已保存: 加载 ${count} 条历史消息`);
}

// ================== Context Menu ==================

function showContextMenu(e, msgElement) {
    e.preventDefault();
    selectedMessageData = {
        messageId: msgElement.dataset.messageId,
        senderId: msgElement.dataset.senderId,
        time: parseInt(msgElement.dataset.time) || 0,
        rawMessage: msgElement.dataset.rawMessage,
        element: msgElement
    };

    const menu = document.getElementById('message-context-menu');
    menu.style.display = 'block';
    menu.style.left = e.pageX + 'px';
    menu.style.top = e.pageY + 'px';

    // Show/hide recall based on ownership and time (2 min limit)
    const recallItem = document.getElementById('recall-menu-item');
    const isSelf = selectedMessageData.senderId === CURRENT_USER_ID;
    const withinTimeLimit = (Date.now() / 1000 - selectedMessageData.time) < 120;
    recallItem.style.display = (isSelf && withinTimeLimit) ? 'flex' : 'none';
}

// Hide context menu on click outside
document.addEventListener('click', () => {
    document.getElementById('message-context-menu').style.display = 'none';
});

function copyMessage() {
    if (selectedMessageData?.rawMessage) {
        navigator.clipboard.writeText(selectedMessageData.rawMessage);
        log("已复制到剪贴板");
    }
    document.getElementById('message-context-menu').style.display = 'none';
}

function replyMessage() {
    if (selectedMessageData?.rawMessage) {
        const input = document.getElementById('msg-input');
        input.value = `[回复] ${selectedMessageData.rawMessage.substring(0, 30)}...\n`;
        input.focus();
        // Store reply target
        input.dataset.replyMsgId = selectedMessageData.messageId;
    }
    document.getElementById('message-context-menu').style.display = 'none';
}

async function recallMessage() {
    if (!selectedMessageData?.messageId) {
        alert("无法撤回：消息ID不存在");
        return;
    }
    try {
        const res = await fetch(`${API_BASE}/api/message/recall`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message_id: selectedMessageData.messageId })
        });
        if (res.ok) {
            selectedMessageData.element?.remove();
            log("消息已撤回");
        } else {
            alert("撤回失败");
        }
    } catch (e) {
        alert("撤回失败: " + e.message);
    }
    document.getElementById('message-context-menu').style.display = 'none';
}

function deleteMessage() {
    selectedMessageData?.element?.remove();
    log("消息已从视图中删除");
    document.getElementById('message-context-menu').style.display = 'none';
}

function forwardMessage() {
    alert("转发功能开发中...");
    document.getElementById('message-context-menu').style.display = 'none';
}

function favoriteMessage() {
    if (selectedMessageData?.rawMessage) {
        const favorites = JSON.parse(localStorage.getItem('favorites') || '[]');
        favorites.push({
            content: selectedMessageData.rawMessage,
            time: new Date().toISOString(),
            senderId: selectedMessageData.senderId
        });
        localStorage.setItem('favorites', JSON.stringify(favorites));
        log("已收藏");
    }
    document.getElementById('message-context-menu').style.display = 'none';
}

function multiSelectMessage() {
    alert("多选功能开发中...");
    document.getElementById('message-context-menu').style.display = 'none';
}

// ================== Profile Card ==================

async function showProfileCard(userId) {
    if (!userId || userId === "null" || userId === "undefined") return;

    // Show loading state
    document.getElementById('pc-avatar').src = `http://q1.qlogo.cn/g?b=qq&nk=${userId}&s=100`;
    document.getElementById('pc-name').textContent = '加载中...';
    document.getElementById('pc-qq').textContent = `QQ: ${userId}`;
    document.getElementById('pc-signature').textContent = '';
    document.getElementById('pc-sex').textContent = '-';
    document.getElementById('pc-age').textContent = '-';
    document.getElementById('pc-level').textContent = '-';
    document.getElementById('profile-card-modal').style.display = 'block';

    try {
        const res = await fetch(`${API_BASE}/api/user/stranger?user_id=${userId}`);
        const data = await res.json();

        // Log response for debugging
        console.log("Profile data:", data);

        // Handle various field name formats from different OneBot implementations
        const nickname = data.nickname || data.nick || data.user_name || userId;
        const sex = data.sex || data.gender || '';
        const age = data.age || data.user_age || 0;
        const level = data.level || data.user_level || data.qqLevel || 0;
        const sign = data.sign || data.signature || data.personal_sign || data.qid || '';

        document.getElementById('pc-name').textContent = nickname;
        document.getElementById('pc-signature').textContent = sign;
        document.getElementById('pc-sex').textContent = sex === 'male' ? '男' : sex === 'female' ? '女' : sex || '-';
        document.getElementById('pc-age').textContent = age || '-';
        document.getElementById('pc-level').textContent = level || '-';
    } catch (e) {
        log("获取用户信息失败: " + e.message);
        document.getElementById('pc-name').textContent = userId;
    }
}

function sendMessageTo() {
    const qq = document.getElementById('pc-qq').textContent.replace('QQ: ', '');
    document.getElementById('target-qq').value = qq;
    document.getElementById('is-group').checked = false;
    closeModal('profile-card-modal');
    updateDashboard();
}

// ================== File Download ==================

async function downloadFile(fileId, fileName) {
    if (!fileId) {
        alert("文件ID无效");
        return;
    }
    try {
        const res = await fetch(`${API_BASE}/api/file/download/${fileId}`);
        if (res.ok) {
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = fileName || 'download';
            a.click();
            URL.revokeObjectURL(url);
        } else {
            alert("下载失败");
        }
    } catch (e) {
        alert("下载失败: " + e.message);
    }
}

/* ... existing fetchModels ... */

// Poll status every 5 seconds
setInterval(checkNapCatStatus, 5000);

async function fetchModels() {
    const baseUrl = document.getElementById('llm-base').value;
    const apiKey = document.getElementById('llm-key').value;

    if (!baseUrl || !apiKey) {
        alert("请输入 Base URL 和 API Key");
        return;
    }

    log("获取模型列表中...");
    try {
        const res = await fetch(`${API_BASE}/api/llm/models`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ base_url: baseUrl, api_key: apiKey, model: "" })
        });
        const data = await res.json();
        const select = document.getElementById('llm-model');
        select.innerHTML = '<option value="" disabled selected>选择模型</option>';
        data.models.forEach(m => {
            const opt = document.createElement('option');
            opt.value = m;
            opt.textContent = m;
            select.appendChild(opt);
        });
        log(`获取到 ${data.models.length} 个模型`);
    } catch (e) {
        log(`获取模型失败: ${e}`);
    }
}

async function saveLLMConfig() {
    const baseUrl = document.getElementById('llm-base').value;
    const apiKey = document.getElementById('llm-key').value;
    const model = document.getElementById('llm-model').value;

    if (!model) {
        alert("请选择模型");
        return;
    }

    await fetch(`${API_BASE}/api/llm/save`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey, model: model })
    });
    log("LLM 配置已保存");
}

async function updateDashboard() {
    const targetQQ = document.getElementById('target-qq').value;
    const isGroup = document.getElementById('is-group').checked;
    const groupId = document.getElementById('group-id').value;
    const knownInfo = document.getElementById('known-info').value;

    // Settings
    const days = parseInt(document.getElementById('setting-days').value) || 7;
    const msgCount = parseInt(document.getElementById('setting-msg-count').value) || 50;
    const qzoneCount = parseInt(document.getElementById('setting-qzone-count').value) || 10;

    // For groups, we don't need targetQQ, just group_id
    if (!targetQQ && !isGroup) {
        alert("请输入目标 QQ");
        return;
    }
    if (isGroup && !groupId) {
        alert("群聊模式需要群号");
        return;
    }

    log("开始爬取与分析...");

    try {
        const res = await fetch(`${API_BASE}/api/dashboard/update`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                target_id: isGroup ? 0 : parseInt(targetQQ),  // 0 for groups (no specific user)
                is_group: isGroup,
                group_id: groupId ? parseInt(groupId) : null,
                known_info: knownInfo,
                settings: {
                    history_days: days,
                    context_msg_count: msgCount,
                    qzone_count: qzoneCount
                }
            })
        });

        const data = await res.json();
        renderDashboard(data);
        log("分析完成");
    } catch (e) {
        log(`分析失败: ${e}`);
    }
}

function renderDashboard(data) {
    console.log("Rendering Dashboard. CURRENT_USER_ID:", CURRENT_USER_ID);

    // Store data for later export
    window.currentDashboardData = data;

    // 1. Profile (enhanced with avatar_url if available)
    document.getElementById('profile-name').textContent = data.profile.nickname || data.profile.user_id || "Unknown";
    const signature = data.profile.signature || `${data.profile.sex || '未知'} · ${data.profile.age || '?'}岁`;
    document.getElementById('profile-bio').textContent = signature;
    const profileAvatarUrl = data.profile.avatar_url || `http://q1.qlogo.cn/g?b=qq&nk=${data.profile.user_id || document.getElementById('target-qq').value}&s=640`;
    document.getElementById('profile-avatar').style.background = `url('${profileAvatarUrl}') center/cover`;

    // 2. Analysis
    if (data.analysis) {
        document.getElementById('analysis-personality').textContent = data.analysis.personality || "暂无";
        document.getElementById('analysis-interests').textContent = data.analysis.interests || "暂无";
        document.getElementById('analysis-emotion').textContent = data.analysis.emotion || "暂无";
    }

    // 3. Topics as clickable cards
    const topicsContainer = document.getElementById('suggested-topics');
    topicsContainer.innerHTML = "";
    if (data.topics && Array.isArray(data.topics) && data.topics.length > 0) {
        data.topics.forEach((topic, index) => {
            const card = document.createElement('div');
            card.className = 'topic-card';
            card.textContent = typeof topic === 'string' ? topic : (topic.title || topic.topic || JSON.stringify(topic));
            card.onclick = () => showTopicDetail(topic, index);
            topicsContainer.appendChild(card);
        });
    } else {
        topicsContainer.innerHTML = '<div class="topic-card-placeholder">暂无推荐话题</div>';
    }

    // 4. Chat History
    const chatContainer = document.getElementById('chat-messages');
    chatContainer.innerHTML = "";

    // Render crawled messages
    if (data.recent_chats && data.recent_chats.length > 0) {
        data.recent_chats.forEach(msg => {
            const senderId = msg.sender ? String(msg.sender.user_id) : null;

            // Reliable self-check: compare stringified IDs
            const isSelf = senderId && CURRENT_USER_ID && (senderId === CURRENT_USER_ID);

            // Format timestamp
            const timestamp = msg.time ? formatMessageTime(msg.time) : "";

            const div = document.createElement('div');
            div.className = `message-wrapper ${isSelf ? 'sent' : 'received'}`;
            div.dataset.messageId = msg.message_id || "";
            div.dataset.senderId = senderId;
            div.dataset.time = msg.time || "";
            div.dataset.rawMessage = msg.raw_message || "";

            // Use rich content renderer
            const contentHtml = renderMessageContent(msg.message, msg.raw_message);
            const avatarUrl = senderId ? `http://q1.qlogo.cn/g?b=qq&nk=${senderId}&s=100` : "";
            const senderName = msg.sender?.nickname || senderId || "";

            // Group member badges
            let badgeHtml = "";
            if (msg.sender?.role === "owner") {
                badgeHtml = `<span class="member-badge badge-owner">群主</span>`;
            } else if (msg.sender?.role === "admin") {
                badgeHtml = `<span class="member-badge badge-admin">管理员</span>`;
            }
            if (msg.sender?.level) {
                badgeHtml += `<span class="member-badge badge-level">LV${msg.sender.level}</span>`;
            }

            div.innerHTML = `
                <div class="message-avatar" style="background-image:url('${avatarUrl}'); background-size:cover;" onclick="showProfileCard('${senderId}')"></div>
                <div class="message-content">
                    <div class="message-sender-name">${isSelf ? '' : escapeHtml(senderName)}${badgeHtml}</div>
                    <div class="message-bubble">${contentHtml}</div>
                    ${timestamp ? `<div class="message-meta">${timestamp}</div>` : ''}
                </div>
            `;

            // Add context menu listener
            div.addEventListener('contextmenu', (e) => showContextMenu(e, div));

            chatContainer.appendChild(div);
        });
        chatContainer.scrollTop = chatContainer.scrollHeight;

        // Start lazy loading images
        observeLazyImages(chatContainer);
    } else {
        chatContainer.innerHTML = `<div style="text-align:center;color:#999;margin-top:20px">没有更多消息</div>`;
    }
}

function displayUserInfo(data) {
    const container = document.getElementById('napcat-status');
    container.innerHTML = `
        <div id="napcat-user-info" data-uid="${data.user_id}" style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
            <img src="http://q1.qlogo.cn/g?b=qq&nk=${data.user_id}&s=100" style="width:40px;height:40px;border-radius:50%">
            <div>
                <div style="font-weight:bold">${data.nickname}</div>
                <div style="font-size:0.8em;color:#666">${data.user_id}</div>
            </div>
        </div>
    `;
    container.classList.remove('offline');
    container.classList.add('online');
}

async function fetchUserInfo() {
    try {
        const res = await fetch(`${API_BASE}/api/user/info`);
        const data = await res.json();
        if (data.user_id) {
            displayUserInfo(data);
            fetchContacts();
        }
    } catch (e) { }
}

async function fetchContacts() {
    try {
        const res = await fetch(`${API_BASE}/api/contacts`);
        const data = await res.json();
        renderContacts(data);
    } catch (e) {
        log("获取联系人失败");
    }
}

// --- Chat Functionality ---
function toggleEmojiPicker() {
    const p = document.getElementById('emoji-picker');
    p.style.display = p.style.display === 'none' ? 'grid' : 'none';
}

function insertEmoji(char) {
    const input = document.getElementById('chat-input');
    input.value += char;
}

async function sendMessage() {
    const targetQQ = document.getElementById('target-qq').value;
    const isGroup = document.getElementById('is-group').checked;
    const content = document.getElementById('chat-input').value;

    if (!targetQQ || !content) return;

    try {
        const res = await fetch(`${API_BASE}/api/send`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                target_id: parseInt(targetQQ),
                is_group: isGroup,
                type: "text",
                content: content
            })
        });
        const d = await res.json();
        // Optimistically append message? Or wait for crawl to refresh?
        // Let's manually append for UI responsiveness
        appendLocalMessage(content, "text");
        document.getElementById('chat-input').value = "";

        // Refresh analysis/history after short delay?
        setTimeout(() => updateDashboard(), 2000);
    } catch (e) {
        log("发送失败: " + e);
    }
}

async function handleImageUpload(event) {
    const file = event.target.files[0];
    if (!file) return;

    const targetQQ = document.getElementById('target-qq').value;
    const isGroup = document.getElementById('is-group').checked;
    if (!targetQQ) {
        alert("请先选择聊天目标");
        return;
    }

    log(`正在上传图片: ${file.name}`);
    const formData = new FormData();
    formData.append('file', file);

    try {
        const uploadRes = await fetch(`${API_BASE}/api/upload`, {
            method: 'POST',
            body: formData
        });
        const uploadData = await uploadRes.json();
        if (uploadData.path) {
            // Now send the image message
            const sendRes = await fetch(`${API_BASE}/api/send`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    target_id: parseInt(targetQQ),
                    is_group: isGroup,
                    type: "image",
                    content: uploadData.path
                })
            });
            const sendData = await sendRes.json();
            if (sendData.status === 'ok') {
                appendLocalMessage(`[图片: ${file.name}]`, "image");
                log("图片发送成功");
            } else {
                log("图片发送失败: " + JSON.stringify(sendData));
            }
        } else {
            log("图片上传失败");
        }
    } catch (e) {
        log("图片上传错误: " + e);
    }
    // Reset input
    event.target.value = '';
}

function appendLocalMessage(content, type) {
    const chatContainer = document.getElementById('chat-messages');
    const div = document.createElement('div');
    div.className = 'message-wrapper sent';
    div.innerHTML = `
        <div class="message-bubble">${content}</div>
        <div class="message-avatar" style="background:#ddd"></div>
    `;
    chatContainer.appendChild(div);
    chatContainer.scrollTop = chatContainer.scrollHeight;
}

// Store contacts for search
let allContactsCache = [];

function renderContacts(data, filter = "") {
    const listContainer = document.getElementById('chat-list-container');
    listContainer.innerHTML = "";

    const allContacts = [...(data.friends || []), ...(data.groups || [])];

    // Cache for search
    if (!filter) {
        allContactsCache = allContacts;
    }

    // Apply search filter
    const filterLower = filter.toLowerCase();
    const filteredContacts = filter
        ? allContacts.filter(c => {
            const name = (c.group_name || c.nickname || "").toLowerCase();
            const remark = (c.remark || "").toLowerCase();
            const id = String(c.group_id || c.user_id);
            return name.includes(filterLower) || remark.includes(filterLower) || id.includes(filterLower);
        })
        : allContacts;

    filteredContacts.forEach(c => {
        const isGroup = !!c.group_id;
        const id = isGroup ? c.group_id : c.user_id;
        const baseName = isGroup ? c.group_name : c.nickname;
        // Show remark in parentheses if different from nickname
        const displayName = (!isGroup && c.remark && c.remark !== c.nickname)
            ? `${baseName} (${c.remark})`
            : baseName;
        const sub = isGroup ? `Group (${c.member_count || 0})` : `User (${c.user_id})`;
        const avatar = isGroup
            ? `http://p.qlogo.cn/gh/${id}/${id}/100/`
            : `http://q1.qlogo.cn/g?b=qq&nk=${id}&s=100`;

        const item = document.createElement('div');
        item.className = 'chat-list-item';
        item.innerHTML = `
            <div class="avatar" style="background-image:url('${avatar}');background-size:cover;"></div>
            <div class="chat-info">
                <div class="name">${escapeHtml(displayName)}</div>
                <div class="last-msg">${sub}</div>
            </div>
        `;
        item.onclick = (e) => selectContact(c, e.currentTarget);
        listContainer.appendChild(item);
    });

    if (filteredContacts.length === 0 && filter) {
        listContainer.innerHTML = `<div style="text-align:center;color:#999;padding:20px;">未找到匹配的联系人</div>`;
    }
}

// Search contacts
function searchContacts(query) {
    renderContacts({ friends: allContactsCache.filter(c => !c.group_id), groups: allContactsCache.filter(c => c.group_id) }, query);
}

function selectContact(c, el) {
    const isGroup = !!c.group_id;
    const name = isGroup ? c.group_name : c.nickname;
    const id = isGroup ? c.group_id : c.user_id;

    document.getElementById('target-qq').value = isGroup ? "" : id;
    document.getElementById('is-group').checked = isGroup;
    document.getElementById('group-id').value = isGroup ? id : "";
    document.getElementById('chat-title').textContent = name;

    // Highlight active contact
    document.querySelectorAll('.chat-list-item').forEach(e => e.classList.remove('active'));
    if (el) el.classList.add('active');

    // Auto-fetch history (Crawler analysis)
    updateDashboard();
}


// Global Drag Logic & Console Resize
document.addEventListener('DOMContentLoaded', () => {
    // 1. Column Resizers
    const sidebar = document.querySelector('.sidebar');
    const chatWindow = document.querySelector('.chat-window-container');
    const analysisPanel = document.querySelector('.analysis-panel');

    const resizer1 = document.createElement('div');
    resizer1.className = 'resizer';
    sidebar.parentNode.insertBefore(resizer1, chatWindow);

    const resizer2 = document.createElement('div');
    resizer2.className = 'resizer';
    chatWindow.parentNode.insertBefore(resizer2, analysisPanel);

    // 2. Console Resizer
    const consoleDiv = document.getElementById('console-output');
    const consoleResizer = document.getElementById('console-resizer');

    // Column Resize Function
    const makeResizable = (resizer, prevSibling, nextSibling) => {
        let x = 0;
        let prevWidth = 0;
        const down = (e) => {
            x = e.clientX;
            prevWidth = prevSibling.getBoundingClientRect().width;
            document.addEventListener('mousemove', move);
            document.addEventListener('mouseup', up);
            resizer.style.background = '#0099ff';
        };
        const move = (e) => {
            const dx = e.clientX - x;
            prevSibling.style.width = `${prevWidth + dx}px`;
        };
        const up = () => {
            document.removeEventListener('mousemove', move);
            document.removeEventListener('mouseup', up);
            resizer.style.background = '';
        };
        resizer.addEventListener('mousedown', down);
    };

    // Right Panel Resize
    const makeResizableRight = (resizer, rightPanel) => {
        let x = 0;
        let w = 0;
        const down = (e) => {
            x = e.clientX;
            w = rightPanel.getBoundingClientRect().width;
            document.addEventListener('mousemove', move);
            document.addEventListener('mouseup', up);
            resizer.style.background = '#0099ff';
        };
        const move = (e) => {
            const dx = e.clientX - x;
            rightPanel.style.width = `${w - dx}px`;
        };
        const up = () => {
            document.removeEventListener('mousemove', move);
            document.removeEventListener('mouseup', up);
            resizer.style.background = '';
        };
        resizer.addEventListener('mousedown', down);
    }

    // Console Resize (Vertical)
    const makeConsoleResizable = (resizer, consoleEl) => {
        let y = 0;
        let h = 0;
        const down = (e) => {
            y = e.clientY;
            h = consoleEl.getBoundingClientRect().height;
            document.addEventListener('mousemove', move);
            document.addEventListener('mouseup', up);
            resizer.style.background = '#0099ff';
        };
        const move = (e) => {
            const dy = e.clientY - y;
            consoleEl.style.height = `${h + dy}px`;
        };
        const up = () => {
            document.removeEventListener('mousemove', move);
            document.removeEventListener('mouseup', up);
            resizer.style.background = '';
        };
        resizer.addEventListener('mousedown', down);
    }

    makeResizable(resizer1, sidebar, chatWindow);
    makeResizableRight(resizer2, analysisPanel);
    if (consoleResizer && consoleDiv) {
        makeConsoleResizable(consoleResizer, consoleDiv);
    }
});

// ================== User Data Management ==================

let currentSelectedTopic = null;
let userNoteImages = [];

function openUserDataModal() {
    const targetQQ = document.getElementById('target-qq').value;
    if (!targetQQ && !window.currentDashboardData?.profile?.user_id) {
        alert('请先输入目标QQ账号并运行分析');
        return;
    }

    const userId = window.currentDashboardData?.profile?.user_id || targetQQ;
    loadUserDataModal(userId);
    document.getElementById('user-data-modal').style.display = 'flex';
}

async function loadUserDataModal(userId) {
    try {
        const res = await fetch(`${API_BASE}/api/user/data/${userId}`);
        const data = await res.json();

        // Populate profile tab
        const profileDisplay = document.getElementById('user-profile-display');
        if (data.profile && Object.keys(data.profile).length > 0) {
            profileDisplay.innerHTML = `
                ${data.profile.avatar_url ? `<img src="${data.profile.avatar_url}" class="profile-avatar-large">` : ''}
                <div class="profile-info-row"><span class="label">QQ号</span><span class="value">${data.profile.user_id || '-'}</span></div>
                <div class="profile-info-row"><span class="label">昵称</span><span class="value">${data.profile.nickname || '-'}</span></div>
                <div class="profile-info-row"><span class="label">性别</span><span class="value">${data.profile.sex || '-'}</span></div>
                <div class="profile-info-row"><span class="label">年龄</span><span class="value">${data.profile.age || '-'}</span></div>
                <div class="profile-info-row"><span class="label">签名</span><span class="value">${data.profile.signature || '-'}</span></div>
                <div class="profile-info-row"><span class="label">空间链接</span><span class="value"><a href="${data.profile.qzone_url || '#'}" target="_blank">访问空间</a></span></div>
            `;
        }

        // Populate history tab
        const historyDisplay = document.getElementById('user-history-display');
        if (data.chat_history && data.chat_history.length > 0) {
            historyDisplay.innerHTML = data.chat_history.map(msg => `
                <div class="history-item">
                    <span class="sender">${msg.sender?.nickname || msg.sender?.user_id || '未知'}</span>
                    <span class="time">${msg.time ? new Date(msg.time * 1000).toLocaleString() : ''}</span>
                    <div>${escapeHtml(msg.raw_message || '')}</div>
                </div>
            `).join('');
        } else {
            historyDisplay.innerHTML = '<p>暂无聊天记录</p>';
        }

        // Populate analysis tab
        const analysisDisplay = document.getElementById('user-analysis-display');
        if (data.analysis && Object.keys(data.analysis).length > 0) {
            analysisDisplay.innerHTML = `
                <div class="data-section"><h4>性格</h4><p>${data.analysis.personality || '暂无'}</p></div>
                <div class="data-section"><h4>兴趣</h4><p>${data.analysis.interests || '暂无'}</p></div>
                <div class="data-section"><h4>情绪</h4><p>${data.analysis.emotion || '暂无'}</p></div>
                <div class="data-section"><h4>总结</h4><p>${data.analysis.summary || '暂无'}</p></div>
            `;
        }

        // Populate notes tab
        if (data.notes) {
            document.getElementById('user-note-input').value = data.notes.note || '';
            userNoteImages = data.notes.images || [];
            renderNoteImages();
        }

    } catch (e) {
        console.error('Failed to load user data:', e);
    }
}

function switchDataTab(tabId) {
    // Remove active class from all tabs and buttons
    document.querySelectorAll('.data-tab').forEach(tab => tab.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));

    // Activate selected tab
    document.getElementById(tabId).classList.add('active');

    // Activate corresponding button
    document.querySelectorAll('.tab-btn').forEach(btn => {
        if (btn.textContent.includes(tabId.replace('-tab', '').replace('profile', '基本').replace('history', '聊天').replace('analysis', '分析').replace('notes', '备注'))) {
            btn.classList.add('active');
        }
    });

    // Fix: directly find the clicked button
    event.target.classList.add('active');
}

async function exportCurrentUserData() {
    const targetQQ = document.getElementById('target-qq').value;
    const userId = window.currentDashboardData?.profile?.user_id || targetQQ;

    if (!userId) {
        alert('请先输入目标QQ账号');
        return;
    }

    try {
        const res = await fetch(`${API_BASE}/api/user/data/${userId}/export`);
        const blob = await res.blob();

        // Create download link
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `user_${userId}_data.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        window.URL.revokeObjectURL(url);

        log(`用户 ${userId} 数据已导出`);
    } catch (e) {
        log(`导出失败: ${e}`);
        alert('导出失败: ' + e.message);
    }
}

async function saveUserNote() {
    const targetQQ = document.getElementById('target-qq').value;
    const userId = window.currentDashboardData?.profile?.user_id || parseInt(targetQQ);
    const note = document.getElementById('user-note-input').value;

    if (!userId) {
        alert('请先输入目标QQ账号');
        return;
    }

    try {
        const res = await fetch(`${API_BASE}/api/user/notes`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                user_id: userId,
                note: note,
                images: userNoteImages
            })
        });

        if (res.ok) {
            alert('备注已保存');
            log(`用户 ${userId} 备注已保存`);
        } else {
            throw new Error('保存失败');
        }
    } catch (e) {
        log(`保存备注失败: ${e}`);
        alert('保存失败: ' + e.message);
    }
}

function handleNoteFileUpload(event) {
    const file = event.target.files[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = (e) => {
        userNoteImages.push(e.target.result);
        renderNoteImages();
    };
    reader.readAsDataURL(file);
}

function renderNoteImages() {
    const container = document.getElementById('note-images-preview');
    container.innerHTML = userNoteImages.map((img, idx) => `
        <img src="${img}" class="note-image-preview" onclick="removeNoteImage(${idx})">
    `).join('');
}

function removeNoteImage(index) {
    userNoteImages.splice(index, 1);
    renderNoteImages();
}

// ================== Topic Detail ==================

function showTopicDetail(topic, index) {
    currentSelectedTopic = topic;

    const title = document.getElementById('topic-detail-title');
    const content = document.getElementById('topic-detail-content');

    if (typeof topic === 'string') {
        title.textContent = `推荐话题 ${index + 1}`;
        content.innerHTML = `<p>${escapeHtml(topic)}</p>
            <p style="margin-top:15px;color:#666;font-size:0.9em;">💡 点击下方按钮可复制到聊天输入框</p>`;
    } else {
        title.textContent = topic.title || `话题 ${index + 1}`;
        content.innerHTML = `
            <p>${escapeHtml(topic.content || topic.description || JSON.stringify(topic))}</p>
            ${topic.reason ? `<p style="margin-top:10px;color:#666;"><strong>推荐理由:</strong> ${escapeHtml(topic.reason)}</p>` : ''}
        `;
    }

    document.getElementById('topic-detail-modal').style.display = 'flex';
}

function copyTopicToInput() {
    if (!currentSelectedTopic) return;

    const chatInput = document.getElementById('chat-input');
    const topicText = typeof currentSelectedTopic === 'string'
        ? currentSelectedTopic
        : (currentSelectedTopic.content || currentSelectedTopic.title || '');

    chatInput.value = topicText;
    chatInput.focus();

    closeModal('topic-detail-modal');
    log('话题已复制到输入框');
}
