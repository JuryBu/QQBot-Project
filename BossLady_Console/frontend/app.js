/* ============================================================
   老板娘控制中心 - 前端逻辑
   SPA 路由 + API 调用 + 实时状态更新
   ============================================================ */

const API_BASE = '/api';

// XSS 安全：HTML 转义
function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

// 文件大小格式化
function _formatFileSize(bytes) {
    if (!bytes || bytes <= 0) return '';
    bytes = parseInt(bytes);
    if (bytes < 1024) return bytes + 'B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + 'KB';
    if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + 'MB';
    return (bytes / 1073741824).toFixed(1) + 'GB';
}

// 文件类型图标
function _fileTypeIcon(filename) {
    if (!filename) return '📎';
    const ext = filename.split('.').pop().toLowerCase();
    const icons = {
        pdf: '📕', doc: '📝', docx: '📝', ppt: '📊', pptx: '📊',
        xls: '📗', xlsx: '📗', txt: '📄', md: '📄',
        zip: '📦', rar: '📦', '7z': '📦',
        mp4: '🎬', avi: '🎬', mkv: '🎬', mov: '🎬', webm: '🎬',
        mp3: '🎵', wav: '🎵', flac: '🎵', ogg: '🎵',
        jpg: '🖼️', jpeg: '🖼️', png: '🖼️', gif: '🖼️', webp: '🖼️',
        py: '🐍', js: '📜', html: '🌐', css: '🎨',
    };
    return icons[ext] || '📎';
}
// ============================================================
// SPA 路由
// ============================================================

function navigateTo(page) {
    document.querySelectorAll('.page').forEach(p => p.classList.add('hidden'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));

    const pageEl = document.getElementById(`page-${page}`);
    const navEl = document.querySelector(`[data-page="${page}"]`);

    if (pageEl) {
        pageEl.classList.remove('hidden');
        // 强制重新播放动画
        pageEl.style.animation = 'none';
        pageEl.offsetHeight;
        pageEl.style.animation = null;
    }
    if (navEl) navEl.classList.add('active');

    // 切换页面时清除成本页自动刷新定时器
    if (window._costRefreshTimer) {
        clearInterval(window._costRefreshTimer);
        window._costRefreshTimer = null;
    }

    // 加载对应页面数据
    if (page === 'dashboard') loadDashboard();
    else if (page === 'bot') loadBotPage();
    else if (page === 'models') loadModelsPage();
    else if (page === 'messages') loadMessagesPage();
    else if (page === 'memory') loadMemory();
    else if (page === 'knowledge') loadKnowledge();
    else if (page === 'cost') loadCostPage();
    else if (page === 'sandbox') loadSandbox();
    else if (page === 'settings') loadSettingsPage();
}

// 监听导航点击
document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', (e) => {
        e.preventDefault();
        const page = item.dataset.page;
        window.location.hash = page;
        navigateTo(page);
    });
});

// Hash 路由
window.addEventListener('hashchange', () => {
    const page = window.location.hash.slice(1) || 'dashboard';
    navigateTo(page);
});

// ============================================================
// 仪表盘
// ============================================================

async function loadDashboard() {
    updateTime();

    try {
        // 状态
        const status = await fetch(`${API_BASE}/dashboard/status`).then(r => r.json());

        // QQ BOT 在线 = NapCat HTTP + AstrBot HTTP + OneBot WS 三层全通
        updateIndicator('botIndicator', 'botStatus',
            status.qq_bot?.online, '在线', status.qq_bot?.detail || '离线');
        updateIndicator('napcatIndicator', 'napcatStatus',
            status.napcat?.running, '已连接', '离线');
        updateIndicator('astrbotIndicator', 'astrbotStatus',
            status.astrbot?.running, '运行中', '离线');

        // OneBot WS 告警条
        const wsAlert = document.getElementById('onebotWsAlert');
        if (wsAlert) {
            if (!status.onebot_ws?.connected && (status.napcat?.running || status.astrbot?.running)) {
                wsAlert.style.display = 'block';
                const detail = document.getElementById('onebotWsDetail');
                if (detail) detail.textContent = status.onebot_ws?.detail || 'NapCat→AstrBot 消息链路断开';
            } else {
                wsAlert.style.display = 'none';
            }
        }
    } catch (e) {
        console.warn('状态获取失败:', e);
    }

    try {
        // 统计
        const stats = await fetch(`${API_BASE}/dashboard/stats`).then(r => r.json());

        animateNumber('statMessages', stats.messages?.total || 0);
        animateNumber('statToday', stats.messages?.today || 0);
        animateNumber('statMemory', stats.memory?.total || 0);
        animateNumber('statKnowledge', stats.knowledge?.windows || 0);
        animateNumber('statCheckpoint', stats.checkpoint?.total || 0);
        document.getElementById('statSandbox').textContent =
            `${stats.sandbox?.workspace_mb || 0} MB`;
    } catch (e) {
        console.warn('统计获取失败:', e);
    }

    // 加载消息流
    loadRecentMessages();
}

function updateIndicator(indicatorId, statusId, isOnline, onlineText, offlineText) {
    const indicator = document.getElementById(indicatorId);
    const statusEl = document.getElementById(statusId);
    if (indicator) {
        indicator.className = `status-indicator ${isOnline ? 'online' : 'offline'}`;
    }
    if (statusEl) {
        statusEl.textContent = isOnline ? onlineText : offlineText;
    }
}

function animateNumber(id, target) {
    const el = document.getElementById(id);
    if (!el) return;
    const start = parseInt(el.textContent) || 0;
    const duration = 600;
    const startTime = Date.now();

    function step() {
        const elapsed = Date.now() - startTime;
        const progress = Math.min(elapsed / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        el.textContent = Math.round(start + (target - start) * eased).toLocaleString();
        if (progress < 1) requestAnimationFrame(step);
    }
    step();
}

// 消息流轮询定时器
let _streamTimer = null;

async function loadRecentMessages() {
    // 清除旧定时器
    if (_streamTimer) clearTimeout(_streamTimer);

    const el = document.getElementById('messageStream');
    const countEl = document.getElementById('streamCount');
    if (!el) return;

    try {
        const data = await fetch(`${API_BASE}/messages/search?limit=25`).then(r => r.json());
        const msgs = data.messages || [];

        if (countEl) countEl.textContent = `最近 ${msgs.length} 条 | 共 ${data.total || 0} 条`;

        if (msgs.length === 0) {
            el.innerHTML = '<p style="color:var(--text-secondary);text-align:center;padding:24px 0">暂无消息记录</p>';
        } else {
            el.innerHTML = msgs.map(m => {
                const icon = m.window_type === 'group' ? '👥' : '👤';
                const time = m.time ? m.time.slice(11, 19) : '';
                let content = escapeHtml(m.content || '').slice(0, 120);
                const recallBadge = m.recalled ? ' <span style="color:var(--danger);font-size:11px">[已撤回]</span>' : '';
                const groupTag = m.group_name ? `<span style="background:rgba(107,92,245,0.12);color:var(--accent);padding:1px 6px;border-radius:3px;font-size:10px;white-space:nowrap">${escapeHtml(m.group_name)}</span>` : '';
                const senderTitle = m.sender_id ? `QQ: ${escapeHtml(m.sender_id)}` : '';
                // === 特殊消息类型渲染 ===
                let specialBadge = '';
                const rawContent = m.content || '';
                if (rawContent === '[转发消息]') {
                    specialBadge = '<span style="background:rgba(78,205,196,0.15);color:#4ecdc4;padding:2px 8px;border-radius:4px;font-size:11px;white-space:nowrap">📨 转发消息</span>';
                    content = '';
                } else if (rawContent.startsWith('[文件:') || (m.files && m.files.length > 0)) {
                    // 文件消息：显示文件名 + 类型图标
                    const fname = (m.files && m.files.length > 0) ? m.files[0].name : rawContent.slice(4, -1);
                    const fsize = (m.files && m.files.length > 0 && m.files[0].size) ? ` (${_formatFileSize(m.files[0].size)})` : '';
                    const ficon = _fileTypeIcon(fname);
                    specialBadge = `<span style="background:rgba(255,165,0,0.12);color:#ffa500;padding:2px 8px;border-radius:4px;font-size:11px;white-space:nowrap">${ficon} ${escapeHtml(fname)}${fsize}</span>`;
                    content = '';
                } else if (rawContent === '[视频]' || rawContent.startsWith('[视频:')) {
                    // 视频消息：如有 URL 则内联 video 标签
                    if (m.video_url) {
                        specialBadge = `<video src="${escapeHtml(m.video_url)}" style="height:48px;max-width:80px;border-radius:4px;cursor:pointer;object-fit:cover;border:1px solid rgba(255,255,255,0.1)" preload="metadata" onclick="this.paused?this.play():this.pause()" onerror="this.outerHTML='<span style=\\'color:#ff6b6b;font-size:11px\\'>🎬 视频(已过期)</span>'" title="点击播放"></video>`;
                    } else {
                        specialBadge = '<span style="background:rgba(255,107,107,0.12);color:#ff6b6b;padding:2px 8px;border-radius:4px;font-size:11px;white-space:nowrap">🎬 视频</span>';
                    }
                    content = '';
                } else if (rawContent.startsWith('[卡片') || rawContent.startsWith('[JSON卡片')) {
                    // 卡片消息：展示标题
                    const title = m.card_title || rawContent.replace(/^\[卡片[:：]?|^\[JSON卡片\]?|\]$/g, '').trim() || '卡片消息';
                    specialBadge = `<span style="background:rgba(107,92,245,0.15);color:var(--accent);padding:2px 8px;border-radius:4px;font-size:11px;white-space:nowrap;max-width:200px;overflow:hidden;text-overflow:ellipsis;display:inline-block;vertical-align:middle" title="${escapeHtml(title)}">🔗 ${escapeHtml(title.slice(0,30))}</span>`;
                    content = '';
                } else if (rawContent === '[语音]' || rawContent.startsWith('[语音:')) {
                    // 语音消息：如有 URL 则内联 audio 播放器
                    if (m.voice_url) {
                        specialBadge = `<span style="display:inline-flex;align-items:center;gap:4px;background:rgba(78,205,196,0.12);padding:2px 8px;border-radius:4px"><span style="cursor:pointer;font-size:14px" onclick="var a=this.nextElementSibling;a.paused?a.play():a.pause();this.textContent=a.paused?'▶️':'⏸️'" title="点击播放">▶️</span><audio src="${escapeHtml(m.voice_url)}" preload="none" style="display:none" onended="this.previousElementSibling.textContent='▶️'" onerror="this.parentElement.outerHTML='<span style=\\'color:#4ecdc4;font-size:11px\\'>🎙️ 语音(已过期)</span>'"></audio><span style="color:#4ecdc4;font-size:11px">语音</span></span>`;
                    } else {
                        specialBadge = '<span style="background:rgba(78,205,196,0.12);color:#4ecdc4;padding:2px 8px;border-radius:4px;font-size:11px;white-space:nowrap">🎙️ 语音</span>';
                    }
                    content = '';
                }
                // 构建内联图片缩略图
                let imgInline = '';
                if (m.image_urls && m.image_urls.length > 0) {
                    // 去掉旧消息中的 [图片] 占位文字
                    content = content.replace(/\[图片\]\s*/g, '').trim();
                    imgInline = m.image_urls.map(u => {
                        if (u.startsWith('images/')) {
                            const src = `${API_BASE}/messages/image/${u.replace('images/', '')}`;
                            return `<img src="${src}" style="height:36px;max-width:64px;border-radius:4px;object-fit:cover;cursor:pointer;border:1px solid rgba(255,255,255,0.1);vertical-align:middle" onclick="window.open('${src}','_blank')" loading="lazy" onerror="this.style.display='none'">`;
                        } else {
                            return `<span style="color:#4ecdc4;font-size:11px" title="CDN图片(可能过期)">🖼️</span>`;
                        }
                    }).join(' ');
                }
                return `<div style="display:flex;gap:8px;padding:8px 4px;border-bottom:1px solid rgba(255,255,255,0.04);font-size:13px;align-items:center">
                    <span style="color:var(--text-secondary);min-width:52px;font-size:12px;font-family:monospace">${time}</span>
                    <span style="min-width:18px">${icon}</span>
                    ${groupTag ? `<span>${groupTag}</span>` : ''}
                    <span style="color:var(--accent);min-width:60px;max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${senderTitle}">${escapeHtml(m.sender_name || m.sender_id || '?')}</span>
                    <span style="color:var(--text-primary);flex:1;word-break:break-all">${specialBadge}${content} ${imgInline}${recallBadge}</span>
                </div>`;
            }).join('');
        }
    } catch (e) {
        console.warn('消息流加载失败:', e);
        if (el) el.innerHTML = '<p style="color:var(--danger)">消息流加载失败</p>';
    }

    // 5 秒后再刷新（仅在 dashboard 页面时）
    const currentPage = window.location.hash.slice(1) || 'dashboard';
    if (currentPage === 'dashboard') {
        _streamTimer = setTimeout(loadRecentMessages, 5000);
    }
}

function updateTime() {
    const el = document.getElementById('currentTime');
    if (el) {
        const now = new Date();
        el.textContent = now.toLocaleString('zh-CN', {
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', second: '2-digit'
        });
    }
}

// ============================================================
// Bot 管理
// ============================================================

async function loadBotPage() {
    // NapCat
    try {
        const data = await fetch(`${API_BASE}/bot/napcat/status`).then(r => r.json());
        const html = `
            <div class="info-row">
                <span class="info-label">运行状态</span>
                <span class="info-value">${data.running ? '🟢 运行中' : '🔴 离线'}</span>
            </div>
            <div class="info-row">
                <span class="info-label">自动登录账号</span>
                <span class="info-value">${escapeHtml(data.auto_login_account || '未配置')}</span>
            </div>
            <div class="info-row">
                <span class="info-label">WebUI Token</span>
                <span class="info-value">${escapeHtml(data.token || '未配置')}</span>
            </div>
            <div class="info-row">
                <span class="info-label">NapCat 目录</span>
                <span class="info-value" style="font-size:12px">${escapeHtml(data.napcat_dir || '未找到')}</span>
            </div>
            <div style="margin-top:12px;display:flex;gap:8px">
                <button class="btn btn-secondary" onclick="restartService('napcat')">🔄 重启 NapCat</button>
                <span id="napcatRestartResult" style="color:var(--text-secondary);font-size:13px;line-height:36px"></span>
            </div>
        `;
        document.getElementById('napcatDetail').innerHTML = html;
    } catch (e) {
        document.getElementById('napcatDetail').innerHTML = '<p style="color:var(--danger)">获取 NapCat 状态失败</p>';
    }

    // AstrBot
    try {
        const data = await fetch(`${API_BASE}/bot/astrbot/status`).then(r => r.json());
        document.getElementById('astrbotDetail').innerHTML = `
            <div class="info-row">
                <span class="info-label">运行状态</span>
                <span class="info-value">${data.running ? '🟢 运行中' : '🔴 离线'}</span>
            </div>
            <div style="margin-top:12px;display:flex;gap:8px">
                <button class="btn btn-secondary" onclick="restartService('astrbot')">🔄 重启 AstrBot</button>
                <span id="astrbotRestartResult" style="color:var(--text-secondary);font-size:13px;line-height:36px"></span>
            </div>
        `;
    } catch (e) {
        document.getElementById('astrbotDetail').innerHTML = '<p style="color:var(--danger)">获取 AstrBot 状态失败</p>';
    }
}

async function restartService(service) {
    const resultEl = document.getElementById(`${service}RestartResult`);
    if (resultEl) resultEl.textContent = '重启中...';
    try {
        const res = await fetch(`${API_BASE}/bot/${service}/restart`, {method: 'POST'}).then(r => r.json());
        if (resultEl) resultEl.textContent = res.success ? `✅ ${res.message}` : `❌ ${res.error}`;
        // 3秒后刷新状态
        setTimeout(() => loadBotPage(), 3000);
    } catch (e) {
        if (resultEl) resultEl.textContent = `❌ ${e.message}`;
    }
}

async function loadNapcatWebUI() {
    try {
        const data = await fetch(`${API_BASE}/bot/napcat/webui-url`).then(r => r.json());
        if (data.url) {
            // Open NapCat WebUI in new tab (iframe cross-origin issues)
            window.open(data.url, '_blank');
        } else {
            alert('NapCat WebUI 未配置或未运行，请确认 NapCat 已启动。');
        }
    } catch (e) {
        alert('NapCat 未运行或配置不存在，请先启动 NapCat。');
    }
}

// ============================================================
// 模型配置
// ============================================================

async function loadModelsPage() {
    // 加载图像模型配置（必须 await，否则 refreshAvailableModels 可能先完成导致竞态）
    await loadImageModel();

    // API Key 状态
    try {
        const data = await fetch(`${API_BASE}/models/api-key`).then(r => r.json());
        const providers = data.providers || [];
        let html = '';
        window._providerId = '';
        for (const p of providers) {
            if (!window._providerId && p.id) window._providerId = p.id;
            const keyDisplay = p.key_count > 0 ? p.keys.join(', ') : '<span style="color:var(--warning)">未配置</span>';
            const typeDisplay = p.type ? `(${p.type})` : '';
            html += `<div class="info-row"><span class="info-label">${escapeHtml(p.id)} ${typeDisplay}</span><span class="info-value">${keyDisplay} | ${p.enabled ? '✅' : '❌'}</span></div>`;
        }
        document.getElementById('apiKeyInfo').innerHTML = html || '<p style="color:var(--text-secondary)">暂无 Provider，请先在下方输入 API Key</p>';
    } catch (e) {
        document.getElementById('apiKeyInfo').innerHTML = '<p style="color:var(--warning)">API Key 信息获取失败</p>';
    }

    // 主模型
    try {
        const data = await fetch(`${API_BASE}/models/main-model`).then(r => r.json());
        if (data.model) {
            document.getElementById('mainModelInfo').innerHTML = `<div class="info-row"><span class="info-label">当前模型</span><span class="info-value" style="color:var(--success)">${escapeHtml(data.model)}</span></div>`;
            // 缓存加载的配置以便动态渲染后回填
            window._savedModelConfig.main = data;
            _setSelectValue('mainModelSelect', data.model);
            updateModelParams('main');
        } else {
            document.getElementById('mainModelInfo').innerHTML = '<p style="color:var(--text-secondary)">尚未配置主模型，请在下方选择</p>';
        }
    } catch (e) {
        document.getElementById('mainModelInfo').innerHTML = '<p style="color:var(--warning)">主模型信息获取失败</p>';
    }

    // Flash Lite
    try {
        const data = await fetch(`${API_BASE}/models/flashlite`).then(r => r.json());
        document.getElementById('flashliteInfo').innerHTML = `<div class="info-row"><span class="info-label">当前模型</span><span class="info-value" style="color:var(--success)">${escapeHtml(data.model || '未设置')}</span></div>`;
        window._savedModelConfig.flashlite = data;
        _setSelectValue('flashliteModelSelect', data.model);
        updateModelParams('flashlite');
    } catch (e) {
        document.getElementById('flashliteInfo').innerHTML = '<p style="color:var(--warning)">Flash Lite 信息获取失败</p>';
    }

    // 工具模型
    try {
        const data = await fetch(`${API_BASE}/models/tool-model`).then(r => r.json());
        document.getElementById('toolModelInfo').innerHTML = data.model
            ? `<div class="info-row"><span class="info-label">当前模型</span><span class="info-value" style="color:var(--success)">${escapeHtml(data.model)}</span></div>`
            : '<p style="color:var(--text-secondary)">尚未配置工具模型，请在下方选择</p>';
        window._savedModelConfig.tool = data;
        _setSelectValue('toolModelSelect', data.model);
        updateModelParams('tool');
        // tool key pool: 初始化本地状态并渲染列表
        window._toolKeyPool = (data.api_keys || []).slice(); // 脱敏展示列表
        window._toolKeyPoolRaw = (data.api_keys_raw || []).slice(); // 原始值列表
        renderToolKeyPool();
    } catch (e) {
        document.getElementById('toolModelInfo').innerHTML = '<p style="color:var(--warning)">工具模型信息获取失败</p>';
    }

    refreshAvailableModels();
}

function _setSelectValue(id, val) {
    if (!val) return;
    const sel = document.getElementById(id);
    for (let i = 0; i < sel.options.length; i++) { if (sel.options[i].value === val) { sel.value = val; return; } }
    const o = document.createElement('option'); o.value = val; o.textContent = val; sel.appendChild(o); sel.value = val;
}

// 全局模型能力缓存
window._modelCapabilities = {};
// 全局已加载配置缓存（用于动态渲染后回填保存值）
window._savedModelConfig = {};

function _populateModelSelects(models) {
    // 构建能力缓存
    for (const m of models) {
        window._modelCapabilities[m.name] = m.capabilities || {};
    }
    for (const id of ['mainModelSelect','flashliteModelSelect','toolModelSelect','imageModelSelect']) {
        const sel = document.getElementById(id); const cur = sel.value;
        while (sel.options.length > 1) sel.options.remove(1);
        // 图像模型只显示有图像生成能力的模型
        const filtered = id === 'imageModelSelect'
            ? models.filter(m => m.hasImageGen)
            : models.filter(m => m.capabilities?.generateContent);
        for (const m of filtered) { const o = document.createElement('option'); o.value = m.name; o.textContent = `${m.name} (${(m.inputTokenLimit||0).toLocaleString()} in)`; sel.appendChild(o); }
        if (cur) sel.value = cur;
        // 图像模型额外保障：如果 cur 回填失败，从缓存恢复
        if (id === 'imageModelSelect' && !sel.value && window._savedModelConfig.image?.model) {
            const savedModel = window._savedModelConfig.image.model;
            let found = false;
            for (let opt of sel.options) { if (opt.value === savedModel) { sel.value = savedModel; found = true; break; } }
            if (!found) { const opt = new Option(savedModel, savedModel, true, true); sel.add(opt, 0); }
        }
    }
    // 触发当前选中模型的参数渲染
    for (const role of ['main','flashlite','tool']) updateModelParams(role);
    updateImageModelParams();
}

// 动态参数渲染：根据模型能力显示/隐藏参数控件
function updateModelParams(role) {
    const selectId = role === 'main' ? 'mainModelSelect' : role === 'flashlite' ? 'flashliteModelSelect' : 'toolModelSelect';
    const containerId = role + 'ModelParams';
    const sel = document.getElementById(selectId);
    const container = document.getElementById(containerId);
    if (!sel || !container) return;

    const modelName = sel.value;
    const caps = window._modelCapabilities[modelName] || {};
    let html = '';

    // Temperature（所有对话模型都有）
    if (caps.temperature || caps.generateContent) {
        const defTemp = role === 'main' ? '0.7' : role === 'flashlite' ? '0.5' : '0.3';
        html += `<div style="width:120px">
            <label style="color:var(--text-secondary);font-size:13px">Temperature</label>
            <input type="number" id="${role}Temperature" class="input-field" value="${defTemp}" min="0" max="2" step="0.1" style="width:100%;margin-top:4px">
        </div>`;
    }

    // Max Tokens（所有对话模型都有）
    if (caps.generateContent) {
        const defTokens = role === 'main' ? '4096' : role === 'flashlite' ? '2048' : '4096';
        html += `<div style="width:120px">
            <label style="color:var(--text-secondary);font-size:13px">Max Tokens</label>
            <input type="number" id="${role}MaxTokens" class="input-field" value="${defTokens}" min="256" max="65536" style="width:100%;margin-top:4px">
        </div>`;
    }

    // Thinking（仅支持思考的模型）
    if (caps.thinking) {
        const defLevel = role === 'tool' ? 'HIGH' : 'MEDIUM';
        const defBudget = role === 'main' ? '8192' : role === 'flashlite' ? '1024' : '4096';
        html += `<div style="width:140px">
            <label style="color:var(--text-secondary);font-size:13px">思考级别</label>
            <select id="${role}ThinkLevel" class="input-field" style="width:100%;margin-top:4px">
                <option value="NONE">NONE</option>
                <option value="LOW">LOW</option>
                <option value="MEDIUM"${defLevel==='MEDIUM'?' selected':''}>MEDIUM</option>
                <option value="HIGH"${defLevel==='HIGH'?' selected':''}>HIGH</option>
            </select>
        </div>
        <div style="width:140px">
            <label style="color:var(--text-secondary);font-size:13px">思考预算(tokens)</label>
            <input type="number" id="${role}ThinkBudget" class="input-field" value="${defBudget}" min="0" step="256" style="width:100%;margin-top:4px">
        </div>`;
    }

    // 无模型选择时的提示
    if (!modelName) {
        html = '<span style="color:var(--text-secondary);font-size:12px;line-height:36px">← 选择模型后显示可配置参数</span>';
    }

    container.innerHTML = html;

    // 自动回填已保存的配置值
    const saved = window._savedModelConfig[role];
    if (saved && modelName) {
        const tempEl = document.getElementById(`${role}Temperature`);
        if (tempEl) {
            const v = role === 'main' ? (saved.temperature ?? 0.7) :
                      role === 'flashlite' ? (saved.temperature ?? 0.5) :
                      (saved.temperature ?? 0.3);
            tempEl.value = v;
        }
        const mtEl = document.getElementById(`${role}MaxTokens`);
        if (mtEl && saved.max_tokens) mtEl.value = saved.max_tokens;

        // thinking 参数
        const tlEl = document.getElementById(`${role}ThinkLevel`);
        const tbEl = document.getElementById(`${role}ThinkBudget`);
        if (tlEl) {
            const level = saved.thinking_level || (role === 'flashlite' ? saved.thinking_config?.thinkingLevel : null);
            if (level) tlEl.value = level;
        }
        if (tbEl) {
            const budget = saved.thinking_budget || saved.thinking_config?.thinkingBudget;
            if (budget) tbEl.value = budget;
        }
    }
}

async function refreshAvailableModels() {
    const c = document.getElementById('modelList');
    c.innerHTML = '<p style="color:var(--text-secondary)">加载可用模型列表中...</p>';
    try {
        const data = await fetch(`${API_BASE}/models/available`).then(r => r.json());
        if (data.error) { c.innerHTML = `<p style="color:var(--warning)">${escapeHtml(data.error)}</p>`; return; }
        const models = data.models || [];
        if (!models.length) { c.innerHTML = '<p style="color:var(--text-secondary)">未找到模型（请先配置 API Key）</p>'; return; }
        _populateModelSelects(models);
        c.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:13px"><tr style="color:var(--text-secondary);border-bottom:1px solid var(--border)"><th style="text-align:left;padding:6px 8px">模型名</th><th style="text-align:right;padding:6px 8px">输入上限</th><th style="text-align:right;padding:6px 8px">输出上限</th></tr>${models.map(m=>`<tr style="border-bottom:1px solid rgba(107,92,245,0.08)"><td style="padding:6px 8px;color:var(--text-primary)">${escapeHtml(m.name)}</td><td style="padding:6px 8px;text-align:right;color:var(--text-secondary)">${(m.inputTokenLimit||0).toLocaleString()}</td><td style="padding:6px 8px;text-align:right;color:var(--text-secondary)">${(m.outputTokenLimit||0).toLocaleString()}</td></tr>`).join('')}</table>`;
    } catch (e) { c.innerHTML = `<p style="color:var(--danger)">加载失败: ${escapeHtml(e.message)}</p>`; }
}

async function saveApiKey() {
    const key = document.getElementById('apiKeyInput').value.trim();
    const r = document.getElementById('apiKeySaveResult');
    if (!key) { r.textContent = '❌ 请输入 API Key'; r.style.color = 'var(--danger)'; return; }
    try {
        const res = await fetch(`${API_BASE}/models/api-key`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider_id:window._providerId||'gemini_pro',keys:[key]})}).then(x=>x.json());
        r.textContent = res.success ? '✅ 已保存，可刷新模型列表' : `❌ ${res.detail||'保存失败'}`;
        r.style.color = res.success ? 'var(--success)' : 'var(--danger)';
        if (res.success) { document.getElementById('apiKeyInput').value = ''; loadModelsPage(); }
    } catch (e) { r.textContent = `❌ ${e.message}`; r.style.color = 'var(--danger)'; }
}

async function saveMainModel() {
    const model = document.getElementById('mainModelSelect').value;
    const r = document.getElementById('mainModelSaveResult');
    if (!model) { r.textContent = '❌ 请选择模型'; r.style.color = 'var(--danger)'; return; }
    try {
        const body = {provider_id:window._providerId||'gemini_pro', model};
        const mtEl = document.getElementById('mainMaxTokens');
        if (mtEl) body.max_tokens = parseInt(mtEl.value) || 4096;
        const tempEl = document.getElementById('mainTemperature');
        if (tempEl) body.temperature = parseFloat(tempEl.value) || 0.7;
        const mTlEl = document.getElementById('mainThinkLevel');
        if (mTlEl) body.thinking_level = mTlEl.value;
        const mTbEl = document.getElementById('mainThinkBudget');
        if (mTbEl) body.thinking_budget = parseInt(mTbEl.value) || 0;
        const res = await fetch(`${API_BASE}/models/main-model`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(x=>x.json());
        r.textContent = res.success ? '✅ 已保存' : `❌ ${res.detail||'失败'}`;
        r.style.color = res.success ? 'var(--success)' : 'var(--danger)';
        if (res.success) loadModelsPage();
    } catch (e) { r.textContent = `❌ ${e.message}`; r.style.color = 'var(--danger)'; }
}

async function saveFlashLite() {
    const r = document.getElementById('flashliteSaveResult');
    try {
        const flBody = {model:document.getElementById('flashliteModelSelect').value||undefined};
        const flBudgetEl = document.getElementById('flashliteThinkBudget');
        if (flBudgetEl) flBody.thinking_budget = parseInt(flBudgetEl.value) || 1024;
        const flLevelEl = document.getElementById('flashliteThinkLevel');
        if (flLevelEl) flBody.thinking_level = flLevelEl.value;
        const res = await fetch(`${API_BASE}/models/flashlite`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(flBody)}).then(x=>x.json());
        r.textContent = res.success ? '✅ 已保存' : `❌ ${res.detail||'失败'}`;
        r.style.color = res.success ? 'var(--success)' : 'var(--danger)';
        if (res.success) loadModelsPage();
    } catch (e) { r.textContent = `❌ ${e.message}`; r.style.color = 'var(--danger)'; }
}

async function saveToolModel() {
    const r = document.getElementById('toolModelSaveResult');
    try {
        // 收集 API Key 池（从交互式列表状态）
        const apiKeys = (window._toolKeyPoolRaw || []).filter(k => k && k.trim());
        const body = {
            model: document.getElementById('toolModelSelect').value || undefined,
        };
        const tLvlEl = document.getElementById('toolThinkLevel');
        if (tLvlEl) body.thinking_level = tLvlEl.value;
        const tBdgEl = document.getElementById('toolThinkBudget');
        if (tBdgEl) body.thinking_budget = parseInt(tBdgEl.value) || 4096;
        body.api_keys = apiKeys;
        const res = await fetch(`${API_BASE}/models/tool-model`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(x=>x.json());
        r.textContent = res.success ? '✅ 已保存' : `❌ ${res.detail||'失败'}`;
        r.style.color = res.success ? 'var(--success)' : 'var(--danger)';
        if (res.success) loadModelsPage();
    } catch (e) { r.textContent = `❌ ${e.message}`; r.style.color = 'var(--danger)'; }
}

// ============================================================
// 工具模型 API Key 池 — 交互式列表管理
// ============================================================

/** 渲染 key 列表 */
function renderToolKeyPool() {
    const list = document.getElementById('toolKeyPoolList');
    const info = document.getElementById('toolApiKeysInfo');
    if (!list) return;
    const pool = window._toolKeyPool || [];
    const raw = window._toolKeyPoolRaw || [];

    if (pool.length === 0) {
        list.innerHTML = '';
        if (info) info.innerHTML = 'ℹ️ 未配置独立 Key，使用主 API Key';
        return;
    }
    if (info) info.innerHTML = `🔑 已配置 <b>${pool.length}</b> 个并发 Key`;

    list.innerHTML = pool.map((masked, i) => {
        const display = raw[i] ? `***${raw[i].slice(-6)}` : masked;
        return `<div style="display:flex;align-items:center;gap:8px;padding:5px 8px;margin-bottom:4px;background:var(--card-bg);border:1px solid var(--border-color);border-radius:6px">
            <span style="color:var(--text-secondary);font-size:12px;min-width:20px">${i + 1}.</span>
            <span style="flex:1;font-family:monospace;font-size:12px;color:var(--text-primary)">${escapeHtml(display)}</span>
            <button class="btn" onclick="editToolKey(${i})"
                style="padding:2px 8px;font-size:11px;background:transparent;border:1px solid var(--border-color);color:var(--text-secondary);cursor:pointer;border-radius:4px">编辑</button>
            <button class="btn" onclick="deleteToolKey(${i})"
                style="padding:2px 8px;font-size:11px;background:transparent;border:1px solid var(--danger);color:var(--danger);cursor:pointer;border-radius:4px">删除</button>
        </div>`;
    }).join('');
}

/** 显示新增输入框 */
function showAddToolKeyInput() {
    document.getElementById('toolKeyPoolInput').style.display = 'block';
    document.getElementById('toolKeyAddBtn').style.display = 'none';
    const inp = document.getElementById('toolKeyNewInput');
    inp.value = '';
    inp.dataset.editIndex = '';
    inp.focus();
}

/** 取消新增 */
function cancelAddToolKey() {
    document.getElementById('toolKeyPoolInput').style.display = 'none';
    document.getElementById('toolKeyAddBtn').style.display = 'block';
    document.getElementById('toolKeyNewInput').dataset.editIndex = '';
}

/** 确认新增或编辑 */
function confirmAddToolKey() {
    const inp = document.getElementById('toolKeyNewInput');
    const val = (inp.value || '').trim();
    if (!val) { cancelAddToolKey(); return; }

    const editIdx = inp.dataset.editIndex;
    if (editIdx !== '' && editIdx !== undefined) {
        // 编辑模式
        const idx = parseInt(editIdx);
        window._toolKeyPool[idx] = `***${val.slice(-6)}`;
        window._toolKeyPoolRaw[idx] = val;
    } else {
        // 新增模式
        window._toolKeyPool.push(`***${val.slice(-6)}`);
        window._toolKeyPoolRaw.push(val);
    }

    cancelAddToolKey();
    renderToolKeyPool();
}

/** 编辑指定 key */
function editToolKey(idx) {
    document.getElementById('toolKeyPoolInput').style.display = 'block';
    document.getElementById('toolKeyAddBtn').style.display = 'none';
    const inp = document.getElementById('toolKeyNewInput');
    inp.value = window._toolKeyPoolRaw[idx] || '';
    inp.dataset.editIndex = idx;
    inp.placeholder = '输入新的 API Key 替换';
    inp.focus();
}

/** 删除指定 key */
function deleteToolKey(idx) {
    window._toolKeyPool.splice(idx, 1);
    window._toolKeyPoolRaw.splice(idx, 1);
    renderToolKeyPool();
}

// ============================================================
// 图像模型配置（三层优先级动态参数渲染）
// 参考 Kaleidoscope 万花筒项目经验 (2026-03-29 实测)
// ============================================================

// 第2层：硬编码注册表——经实测验证的已知模型能力
const IMAGE_MODEL_CAPS = {
    'gemini-2.5-flash-image': {
        aspectRatio: true, imageSize: true,
        thinkingLevel: false, thinkingBudget: false,
        supportedLevels: []
    },
    'gemini-3-pro-image-preview': {
        aspectRatio: true, imageSize: true,
        thinkingLevel: false, thinkingBudget: false,
        supportedLevels: []
    },
    'gemini-3.1-flash-image-preview': {
        aspectRatio: true, imageSize: true,
        thinkingLevel: true, thinkingBudget: false,
        supportedLevels: ['MINIMAL', 'HIGH']
    },
};

// 第3层：启发式推理——根据模型名推断能力
function _heuristicImageCaps(modelName) {
    const n = modelName.toLowerCase();
    const caps = {
        aspectRatio: true, imageSize: true,
        thinkingLevel: false, thinkingBudget: false,
        supportedLevels: []
    };
    // 3.x 系列可能支持 thinkingLevel
    if (/3\.\d/.test(n) && !n.includes('2.5')) {
        caps.thinkingLevel = true;
        caps.supportedLevels = ['MINIMAL', 'LOW', 'MEDIUM', 'HIGH'];
    }
    // 2.5 系列可能支持 thinkingBudget（但目前的2.5-flash-image实测不支持）
    return caps;
}

// 获取模型能力（注册表 > 启发式推理）
function getImageModelCaps(modelName) {
    if (IMAGE_MODEL_CAPS[modelName]) return IMAGE_MODEL_CAPS[modelName];
    return _heuristicImageCaps(modelName);
}

// 图像模型动态参数渲染
function updateImageModelParams() {
    const container = document.getElementById('imageModelParams');
    if (!container) return;

    const sel = document.getElementById('imageModelSelect');
    const modelName = sel ? sel.value : '';

    if (!modelName) {
        container.innerHTML = '<span style="color:var(--text-secondary);font-size:12px;line-height:36px">← 选择模型后显示可配置参数</span>';
        return;
    }

    const caps = getImageModelCaps(modelName);
    let html = '';

    // 宽高比（所有图像模型都支持）
    if (caps.aspectRatio) {
        html += `<div style="width:140px">
            <label style="color:var(--text-secondary);font-size:13px">默认宽高比</label>
            <select id="imageAspectRatio" class="input-field" style="width:100%;margin-top:4px">
                <option value="auto">auto (自动)</option>
                <option value="1:1">1:1</option>
                <option value="16:9">16:9</option>
                <option value="9:16">9:16</option>
                <option value="4:3">4:3</option>
                <option value="3:4">3:4</option>
            </select>
        </div>`;
    }

    // 图像分辨率（实测所有图像模型都支持）
    if (caps.imageSize) {
        html += `<div style="width:120px">
            <label style="color:var(--text-secondary);font-size:13px">图像分辨率</label>
            <select id="imageImageSize" class="input-field" style="width:100%;margin-top:4px">
                <option value="1K">1K (默认)</option>
                <option value="0.5K">0.5K</option>
                <option value="2K">2K</option>
                <option value="4K">4K</option>
            </select>
        </div>`;
    }

    // 生成数量（通用）
    html += `<div style="width:110px">
        <label style="color:var(--text-secondary);font-size:13px">默认生成数</label>
        <select id="imageNumberOfImages" class="input-field" style="width:100%;margin-top:4px">
            <option value="1">1 张</option>
            <option value="2">2 张</option>
            <option value="3">3 张</option>
            <option value="4">4 张</option>
        </select>
    </div>`;

    // 思考级别（仅部分模型支持，且各模型可选值不同）
    if (caps.thinkingLevel && caps.supportedLevels.length > 0) {
        const levelOptions = caps.supportedLevels
            .map(lv => `<option value="${lv}">${lv}</option>`)
            .join('');
        html += `<div style="width:130px">
            <label style="color:var(--text-secondary);font-size:13px">思考级别</label>
            <select id="imageThinkingLevel" class="input-field" style="width:100%;margin-top:4px">
                ${levelOptions}
            </select>
        </div>`;
    }

    // 能力标签
    const tags = [];
    if (caps.aspectRatio) tags.push('比例');
    if (caps.imageSize) tags.push('分辨率');
    if (caps.thinkingLevel) tags.push('思考');
    html += `<div style="width:100%;margin-top:4px">
        <span style="color:var(--text-secondary);font-size:11px">🔍 该模型支持: ${tags.join(' · ') || '基础参数'}</span>
    </div>`;

    container.innerHTML = html;

    // 回填已保存的值
    const saved = window._savedModelConfig.image;
    if (saved) {
        const ar = document.getElementById('imageAspectRatio');
        if (ar && saved.aspect_ratio) ar.value = saved.aspect_ratio;
        const is = document.getElementById('imageImageSize');
        if (is && saved.image_size) is.value = saved.image_size;
        const num = document.getElementById('imageNumberOfImages');
        if (num && saved.number_of_images) num.value = saved.number_of_images;
        const tl = document.getElementById('imageThinkingLevel');
        if (tl && saved.thinking_level) tl.value = saved.thinking_level;
    }
}

async function loadImageModel() {
    try {
        const data = await fetch(`${API_BASE}/models/image-model`).then(r => r.json());
        // 缓存配置用于回填
        window._savedModelConfig.image = data;
        const infoEl = document.getElementById('imageModelInfo');
        if (infoEl) {
            infoEl.innerHTML = `<span style="float:right;color:var(--accent-primary)">${data.model || '未配置'}</span>`;
            infoEl.innerHTML += `<span style="color:var(--text-secondary);font-size:12px">当前模型</span>`;
        }
        const sel = document.getElementById('imageModelSelect');
        if (sel && data.model) {
            let found = false;
            for (let opt of sel.options) {
                if (opt.value === data.model) { opt.selected = true; found = true; }
            }
            if (!found) {
                const opt = new Option(data.model, data.model, true, true);
                sel.add(opt, 0);
            }
        }
        // 触发动态参数渲染（包含回填）
        updateImageModelParams();
    } catch (e) {
        console.error('加载图像模型配置失败:', e);
    }
}

async function saveImageModel() {
    const model = document.getElementById('imageModelSelect')?.value;
    const aspect = document.getElementById('imageAspectRatio')?.value;
    const imageSize = document.getElementById('imageImageSize')?.value;
    const numImages = document.getElementById('imageNumberOfImages')?.value;
    const thinkingLevel = document.getElementById('imageThinkingLevel')?.value;
    const resultEl = document.getElementById('imageModelSaveResult');
    try {
        const body = { model };
        if (aspect) body.default_aspect_ratio = aspect;
        if (imageSize) body.image_size = imageSize;
        if (numImages) body.number_of_images = parseInt(numImages) || 1;
        if (thinkingLevel !== undefined) body.thinking_level = thinkingLevel || '';
        const resp = await fetch(`${API_BASE}/models/image-model`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });
        const data = await resp.json();
        if (resultEl) {
            resultEl.textContent = data.success ? '✅ 已保存' : '❌ 保存失败';
            resultEl.style.color = data.success ? 'var(--accent-secondary)' : 'var(--accent-danger)';
            setTimeout(() => resultEl.textContent = '', 3000);
        }
    } catch (e) {
        if (resultEl) { resultEl.textContent = `❌ ${e.message}`; resultEl.style.color = 'var(--accent-danger)'; }
    }
}


// ============================================================
// Stage 12: 对话内存
// ============================================================

async function loadMessagesPage() {
    // 统计
    try {
        const stats = await fetch(`${API_BASE}/messages/stats`).then(r => r.json());
        animateNumber('msgTotal', stats.total || 0);
        animateNumber('msgToday', stats.today || 0);
        animateNumber('msgRecalled', stats.recalled || 0);
        document.getElementById('msgDbSize').textContent = `${stats.db_size_mb || 0} MB`;
    } catch (e) { console.warn('消息统计失败:', e); }

    // 窗口列表
    try {
        const data = await fetch(`${API_BASE}/messages/windows`).then(r => r.json());
        const el = document.getElementById('windowsList');
        const wins = data.windows || [];
        el.innerHTML = wins.length === 0 ? '<p>暂无对话窗口</p>' : wins.map(w => `
            <div class="info-row" style="cursor:pointer" onclick="searchMessages('${escapeHtml(w.id)}')">
                <span class="info-label">${w.type === 'group' ? '👥' : '👤'} ${escapeHtml(w.id)}</span>
                <span class="info-value">${w.count} 条 | 最后: ${escapeHtml(w.last_msg?.slice(0,10) || '-')}</span>
            </div>
        `).join('');
    } catch (e) { console.warn('窗口加载失败:', e); }

    // CHECKPOINT 历史
    try {
        const cpData = await fetch(`${API_BASE}/data/checkpoint/list`).then(r => r.json());
        const el = document.getElementById('checkpointList');
        const cps = cpData.checkpoints || [];
        if (cps.length === 0) {
            el.innerHTML = `<p style="color:var(--text-secondary)">暂无 CHECKPOINT 记录${cpData.error ? ' (' + escapeHtml(cpData.error) + ')' : ''}</p>`;
        } else {
            el.innerHTML = `<p style="font-size:12px;color:var(--text-secondary);margin-bottom:8px">共 ${cpData.total} 次压缩</p>` +
                cps.map(cp => `
                <div class="info-row" style="cursor:pointer" onclick="viewCheckpoint(${cp.id})">
                    <span class="info-label" style="min-width:140px">
                        ${cp.window_type === 'group' ? '👥' : '👤'} ${escapeHtml(cp.window_id)}
                    </span>
                    <span style="color:var(--accent);font-size:13px;min-width:80px">
                        压缩率 ${cp.compression_ratio ? (cp.compression_ratio * 100).toFixed(0) + '%' : '-'}
                    </span>
                    <span style="color:var(--text-secondary);font-size:12px;min-width:90px">
                        ~${cp.token_estimate || 0} tokens
                    </span>
                    <span class="info-value" style="font-size:12px">
                        ${escapeHtml(cp.created_at?.slice(0,16) || '')}
                    </span>
                </div>
            `).join('');
        }
    } catch (e) { document.getElementById('checkpointList').innerHTML = `<p style="color:var(--danger)">加载失败: ${escapeHtml(e.message)}</p>`; }

    // 消息列表（默认加载最近50条）
    searchMessages('');
}

async function viewCheckpoint(cpId) {
    try {
        const data = await fetch(`${API_BASE}/data/checkpoint/${cpId}`).then(r => r.json());
        const detail = document.getElementById('checkpointDetail');
        const content = document.getElementById('checkpointDetailContent');
        if (data.checkpoint) {
            content.textContent = data.checkpoint.compressed_content || '(空)';
            detail.style.display = 'block';
        } else {
            content.textContent = data.error || '未找到';
            detail.style.display = 'block';
        }
    } catch (e) {
        document.getElementById('checkpointDetailContent').textContent = `加载失败: ${e.message}`;
        document.getElementById('checkpointDetail').style.display = 'block';
    }
}

let _msgOffset = 0;
const _MSG_PAGE_SIZE = 50;

async function searchMessages(windowId, append = false) {
    if (!append) _msgOffset = 0;
    const q = document.getElementById('msgSearchInput')?.value || '';
    const params = new URLSearchParams({q, limit: _MSG_PAGE_SIZE, offset: _msgOffset});
    if (windowId) params.set('window_id', windowId);
    try {
        const data = await fetch(`${API_BASE}/messages/search?${params}`).then(r => r.json());
        const el = document.getElementById('messagesList');
        const msgs = data.messages || [];
        const total = data.total || 0;

        const rowsHtml = msgs.map(m => `
            <div style="display:flex;gap:10px;padding:6px 4px;border-bottom:1px solid var(--border);font-size:13px;align-items:flex-start">
                <span style="min-width:80px;max-width:110px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex-shrink:0;color:var(--accent)">${escapeHtml(m.sender_name || m.sender_id)}</span>
                <span style="flex:1;word-break:break-all;color:var(--text-primary)">${escapeHtml(m.content) || '[图片]'}</span>
                <span style="font-size:11px;color:var(--text-secondary);min-width:85px;flex-shrink:0;text-align:right;white-space:nowrap">${escapeHtml((m.time || '').replace('T',' ').slice(5,16))}</span>
            </div>
        `).join('');

        const loaded = _msgOffset + msgs.length;
        const footerHtml = `<p style="margin-top:8px;font-size:13px;color:var(--text-secondary)">
            已加载 ${loaded} / ${total} 条
            ${loaded < total ? `<button class="btn btn-primary" style="margin-left:12px;padding:4px 16px;font-size:12px" onclick="loadMoreMessages('${windowId || ''}')">加载更多</button>` : ''}
        </p>`;

        if (append) {
            // 移除旧的 footer，追加新行
            const oldFooter = el.querySelector('p:last-child');
            if (oldFooter) oldFooter.remove();
            el.insertAdjacentHTML('beforeend', rowsHtml + footerHtml);
        } else {
            el.innerHTML = msgs.length === 0 ? '<p>无结果</p>' : rowsHtml + footerHtml;
        }
        _msgOffset = loaded;
    } catch (e) { document.getElementById('messagesList').innerHTML = `<p style="color:var(--danger)">搜索失败: ${e.message}</p>`; }
}

function loadMoreMessages(windowId) {
    searchMessages(windowId || '', true);
}

async function cleanupMessages() {
    const days = parseInt(document.getElementById('cleanupDays').value) || 30;
    if (!confirm(`确定要清理 ${days} 天前的消息吗？此操作不可撤销！`)) return;
    try {
        const data = await fetch(`${API_BASE}/messages/cleanup?days=${days}`, {method:'DELETE'}).then(r => r.json());
        document.getElementById('cleanupResult').textContent = `✅ 已清理 ${data.deleted} 条消息`;
        loadMessagesPage();
    } catch (e) { document.getElementById('cleanupResult').textContent = `❌ 清理失败: ${e.message}`; }
}

// ============================================================
// Stage 12: Memory
// ============================================================

async function loadMemory() {
    const q = document.getElementById('memSearchInput')?.value || '';
    try {
        const resp = await fetch(`${API_BASE}/data/memory/list?query=${encodeURIComponent(q)}`);
        if (!resp.ok) {
            throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
        }
        const data = await resp.json();
        if (data.error) {
            // API returned but with error field
            document.getElementById('memList').innerHTML = `<p style="color:var(--warning)">${escapeHtml(data.error)}</p>`;
            animateNumber('memTotal', 0);
            animateNumber('memWorkspaces', 0);
            animateNumber('memPinned', 0);
            return;
        }
        const stats = data.stats || {};
        const mems = data.memories || [];

        // 统计卡片
        animateNumber('memTotal', stats.total || 0);
        const ws = new Set(mems.map(m => m.workspace || 'default'));
        animateNumber('memWorkspaces', ws.size);
        const pinned = mems.filter(m => m.pinned).length;
        animateNumber('memPinned', pinned);

        // 记忆列表
        document.getElementById('memList').innerHTML = mems.length === 0 ? '<p style="color:var(--text-secondary)">暂无记忆</p>' : mems.map(m => {
            let tags = m.tags || [];
            if (typeof tags === 'string') {
                try { tags = JSON.parse(tags); } catch(_) { tags = tags ? [tags] : []; }
            }
            if (!Array.isArray(tags)) tags = [];
            const tagBadges = tags.slice(0,5).map(t => `<span style="background:rgba(107,92,245,0.15);color:var(--accent);padding:2px 8px;border-radius:4px;font-size:11px">${escapeHtml(t)}</span>`).join(' ');
            const memId = escapeHtml(m.id || '');
            const title = escapeHtml(m.title || m.id || '无标题');
            const isPinned = m.pinned ? '📌 ' : '';
            const wsName = m.workspace ? `<span style="font-size:11px;color:var(--text-secondary)">[${escapeHtml(m.workspace.split(/[\/\\]/).pop())}]</span>` : '';
            return `
            <div class="info-row" style="cursor:pointer;flex-wrap:wrap;gap:4px" onclick="viewMemory('${memId}')">
                <span class="info-label">${isPinned}${title} ${wsName}</span>
                <span class="info-value" style="display:flex;gap:4px;flex-wrap:wrap">${tagBadges}</span>
            </div>
        `}).join('');
    } catch (e) { document.getElementById('memList').innerHTML = `<p style="color:var(--danger)">加载失败: ${escapeHtml(e.message)}</p>`; }
}

async function viewMemory(memId) {
    try {
        const data = await fetch(`${API_BASE}/data/memory/${memId}`).then(r => r.json());
        const m = data.memory;
        if (m) {
            document.getElementById('memDetail').textContent = JSON.stringify(m, null, 2);
        } else {
            document.getElementById('memDetail').textContent = '记忆不存在';
        }
    } catch (e) { document.getElementById('memDetail').textContent = `加载失败: ${e.message}`; }
}

// ============================================================
// Stage 12: Knowledge
// ============================================================

async function loadKnowledge() {
    // 概览统计
    try {
        const data = await fetch(`${API_BASE}/data/knowledge`).then(r => r.json());
        const st = data.stats || {};
        const ops = st.recent_operations || [];
        document.getElementById('knowledgeStats').innerHTML = `
            <div class="stats-grid" style="grid-template-columns:repeat(4,1fr)">
                <div class="stat-card"><div class="stat-icon">🗂️</div><div class="stat-value">${st.windows || 0}</div><div class="stat-label">活跃窗口</div></div>
                <div class="stat-card"><div class="stat-icon">👤</div><div class="stat-value">${st.profiles || 0}</div><div class="stat-label">用户画像</div></div>
                <div class="stat-card"><div class="stat-icon">⚡</div><div class="stat-value">${st.operations || 0}</div><div class="stat-label">操作记录</div></div>
                <div class="stat-card"><div class="stat-icon">📂</div><div class="stat-value" style="font-size:12px">${escapeHtml(st.source || '未知')}</div><div class="stat-label">数据源</div></div>
            </div>
        `;
        // 操作追踪展示
        let rawHtml = '';
        if (ops.length > 0) {
            rawHtml += '<div style="margin-bottom:12px"><strong>⚡ 最近操作</strong><div style="margin-top:6px;font-size:13px;color:var(--text-secondary)">';
            rawHtml += ops.map((op, i) => `<div style="padding:3px 0;border-bottom:1px solid var(--border)">${ops.length - i}. ${escapeHtml(typeof op === 'string' ? op : JSON.stringify(op))}</div>`).reverse().join('');
            rawHtml += '</div></div>';
        }
        rawHtml += '<details><summary style="cursor:pointer;color:var(--accent)">🔍 原始数据</summary><pre style="margin-top:8px;font-size:12px;max-height:300px;overflow:auto">' + escapeHtml(JSON.stringify(data.data || {}, null, 2)) + '</pre></details>';
        document.getElementById('knowledgeData').innerHTML = rawHtml;
    } catch (e) { document.getElementById('knowledgeData').textContent = `加载失败: ${e.message}`; }

    // 窗口展开详情
    try {
        const wData = await fetch(`${API_BASE}/data/knowledge/windows`).then(r => r.json());
        const el = document.getElementById('knowledgeWindows');
        const wins = wData.windows || [];
        if (wins.length === 0) {
            el.innerHTML = `<p style="color:var(--text-secondary)">暂无窗口数据${wData.error ? ' (' + escapeHtml(wData.error) + ')' : ''}</p>`;
        } else {
            el.innerHTML = wins.map(w => {
                const hasDetail = w.summary || w.mood;
                return `
                <div class="card" style="background:var(--bg-secondary);padding:12px;margin-bottom:8px;border-radius:8px">
                    <div style="display:flex;justify-content:space-between;align-items:center">
                        <strong>${w.window_type === 'group' ? '👥' : '🗂️'} ${escapeHtml(w.window_id)}</strong>
                        ${w.last_update ? `<span style="font-size:11px;color:var(--text-secondary)">${escapeHtml(w.last_update)}</span>` : ''}
                    </div>
                    ${w.summary ? `<p style="font-size:13px;color:var(--text-secondary);margin-top:6px">${escapeHtml(w.summary.slice(0,200))}${w.summary.length > 200 ? '...' : ''}</p>` : ''}
                    ${w.mood ? `<p style="font-size:12px;margin-top:4px">🎭 氛围: <span style="color:var(--accent)">${escapeHtml(w.mood)}</span></p>` : ''}
                    ${w.active_users?.length ? `<p style="font-size:12px;margin-top:4px">👤 活跃用户: ${w.active_users.map(u => escapeHtml(u)).join(', ')}</p>` : ''}
                    ${w.record_count !== undefined ? `<p style="font-size:12px;margin-top:4px">📊 记录数: ${w.record_count} | 字段: ${(w.columns||[]).join(', ')}</p>` : ''}
                </div>
            `}).join('');
        }
    } catch (e) { document.getElementById('knowledgeWindows').innerHTML = `<p style="color:var(--danger)">加载失败: ${escapeHtml(e.message)}</p>`; }

    // 用户画像渲染
    try {
        const pData = await fetch(`${API_BASE}/data/knowledge/windows`).then(r => r.json());
        const profiles = pData.user_profiles || {};
        const pel = document.getElementById('knowledgeProfiles');
        const keys = Object.keys(profiles);
        if (keys.length === 0) {
            pel.innerHTML = `<p style="color:var(--text-secondary)">暂无用户画像数据</p>`;
        } else {
            // 按互动次数排序
            const sorted = keys.sort((a, b) => (profiles[b].interaction_count || 0) - (profiles[a].interaction_count || 0));
            pel.innerHTML = sorted.map(qq => {
                const p = profiles[qq];
                const nick = p.nickname || qq;
                const count = p.interaction_count || 0;
                const status = p.status || 'active';
                const firstSeen = (p.first_seen || '').slice(0, 10);
                const lastSeen = (p.last_seen || '').slice(0, 10);
                // 支持新结构(facts数组)和旧结构(key_facts字符串数组)
                const facts = p.facts || [];
                const oldFacts = p.key_facts || [];
                const badges = {'pinned': '📌', 'dynamic': '💬', 'archived': '📦'};
                let factsHtml;
                if (facts.length > 0) {
                    factsHtml = facts.map(f => {
                        const cat = f.category || 'dynamic';
                        const badge = badges[cat] || '';
                        const borderColor = cat === 'pinned' ? 'var(--accent)' : cat === 'archived' ? 'var(--text-secondary)' : 'var(--border)';
                        return `<span style="display:inline-block;background:var(--bg-primary);padding:2px 8px;border-radius:12px;margin:2px 4px 2px 0;border:1px solid ${borderColor};font-size:12px">${badge} ${escapeHtml(f.summary || '?')}</span>`;
                    }).join('');
                } else if (oldFacts.length > 0) {
                    factsHtml = oldFacts.map(f => `<span style="display:inline-block;background:var(--bg-primary);padding:2px 8px;border-radius:12px;margin:2px 4px 2px 0;border:1px solid var(--border);font-size:12px">${escapeHtml(f)}</span>`).join('');
                } else {
                    factsHtml = '<span style="font-size:12px;color:var(--text-secondary)">(暂无关键信息)</span>';
                }
                const statusBadge = status === 'archived' ? ' <span style="font-size:10px;padding:1px 6px;border-radius:8px;background:var(--text-secondary);color:var(--bg-primary)">冷卡片</span>' : '';
                return `
                <div class="card" style="background:var(--bg-secondary);padding:12px;margin-bottom:8px;border-radius:8px;border-left:3px solid ${status === 'archived' ? 'var(--text-secondary)' : 'var(--accent)'}">
                    <div style="display:flex;justify-content:space-between;align-items:center">
                        <strong>👤 ${escapeHtml(nick)} <span style="font-size:11px;color:var(--text-secondary)">(QQ:${escapeHtml(qq)})</span>${statusBadge}</strong>
                        <span style="font-size:11px;color:var(--text-secondary)">互动 ${count} 次</span>
                    </div>
                    ${firstSeen ? `<p style="font-size:11px;color:var(--text-secondary);margin-top:4px">首次: ${escapeHtml(firstSeen)} | 最近: ${escapeHtml(lastSeen)}</p>` : ''}
                    <div style="margin-top:6px">${factsHtml}</div>
                </div>
                `;
            }).join('');

        }
    } catch (e) { document.getElementById('knowledgeProfiles').innerHTML = `<p style="color:var(--danger)">加载失败: ${escapeHtml(e.message)}</p>`; }
}

// ============================================================
// Stage 12: Sandbox
// ============================================================

let currentSandboxPath = '';

async function loadSandbox() {
    // 统计
    try {
        const stats = await fetch(`${API_BASE}/data/sandbox/stats`).then(r => r.json());
        document.getElementById('sandboxStats').innerHTML = stats.exists ? `
            <div class="info-row"><span class="info-label">根目录</span><span class="info-value" style="font-size:12px">${escapeHtml(stats.root)}</span></div>
            <div class="info-row"><span class="info-label">Workspace 大小</span><span class="info-value">${stats.workspace_size_mb} MB</span></div>
            <div class="info-row"><span class="info-label">文件数</span><span class="info-value">${stats.workspace_files}</span></div>
        ` : '<p>Sandbox 目录不存在</p>';
    } catch (e) { console.warn('Sandbox 统计失败:', e); }

    // 基础工具列表
    try {
        const toolsData = await fetch(`${API_BASE}/data/sandbox/tools`).then(r => r.json());
        const container = document.getElementById('sandboxToolsList');
        if (container && toolsData.base_tools) {
            // 按类别分组
            const categories = {};
            const categoryIcons = {
                filesystem: '📂', execution: '⚡', web: '🌐', search: '🔍',
                data: '💾', system: '⚙️', media: '🖼️', memory: '🧠', other: '🔧'
            };
            for (const tool of toolsData.base_tools) {
                const cat = tool.category || 'other';
                if (!categories[cat]) categories[cat] = [];
                categories[cat].push(tool);
            }

            let html = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                <span style="color:var(--text-secondary);">共 ${toolsData.base_count} 个基础工具 | ${toolsData.custom_count} 个自定义工具</span>
            </div>`;
            html += '<div class="tools-grid">';
            for (const tool of toolsData.base_tools) {
                const icon = categoryIcons[tool.category] || '🔧';
                const badges = [];
                if (tool.read_only) badges.push('<span class="tool-badge readonly">只读</span>');
                if (tool.parallel) badges.push('<span class="tool-badge parallel">并行</span>');
                html += `<div class="tool-card">
                    <div class="tool-header">
                        <span class="tool-icon">${icon}</span>
                        <span class="tool-name">${escapeHtml(tool.name)}</span>
                    </div>
                    <div class="tool-desc">${escapeHtml(tool.description)}</div>
                    <div class="tool-meta">
                        <span class="tool-category">${escapeHtml(tool.category)}</span>
                        <span class="tool-timeout">${tool.timeout_ms / 1000}s</span>
                        ${badges.join('')}
                    </div>
                </div>`;
            }
            html += '</div>';
            container.innerHTML = html;
        }
    } catch (e) { console.warn('工具列表加载失败:', e); }

    // 文件树
    await loadSandboxTree(currentSandboxPath);
}

async function loadSandboxTree(path) {
    currentSandboxPath = path || '';
    document.getElementById('sandboxPath').textContent = '/' + currentSandboxPath;
    try {
        const data = await fetch(`${API_BASE}/data/sandbox/tree?path=${encodeURIComponent(path)}`).then(r => r.json());
        const files = data.files || [];
        let html = currentSandboxPath ? `<div class="info-row" style="cursor:pointer" onclick="loadSandboxTree('${currentSandboxPath.split('/').slice(0,-1).join('/')}')">⬆️ ..</div>` : '';
        html += files.map(f => {
            if (f.type === 'dir') {
                return `<div class="info-row" style="cursor:pointer" onclick="loadSandboxTree('${f.path}')">📁 ${f.name} <span style="color:var(--text-secondary);font-size:12px">(${f.children} items)</span></div>`;
            } else {
                const size = f.size > 1024 ? `${(f.size/1024).toFixed(1)}KB` : `${f.size}B`;
                return `<div class="info-row" style="cursor:pointer" onclick="viewSandboxFile('${f.path}')">📄 ${f.name} <span style="color:var(--text-secondary);font-size:12px">${size}</span></div>`;
            }
        }).join('');
        document.getElementById('sandboxTree').innerHTML = html || '<p>空目录</p>';
    } catch (e) { document.getElementById('sandboxTree').innerHTML = `<p style="color:var(--danger)">加载失败: ${e.message}</p>`; }
}

async function viewSandboxFile(path) {
    try {
        const data = await fetch(`${API_BASE}/data/sandbox/file?path=${encodeURIComponent(path)}`).then(r => r.json());
        document.getElementById('sandboxFileContent').textContent = data.content || data.error || '无内容';
    } catch (e) { document.getElementById('sandboxFileContent').textContent = `加载失败: ${e.message}`; }
}

// ============================================================
// Stage 12: 系统设置
// ============================================================

async function loadSettingsPage() {
    // 系统信息
    try {
        const info = await fetch(`${API_BASE}/system/info`).then(r => r.json());
        document.getElementById('systemInfo').innerHTML = Object.entries(info).map(([k, v]) =>
            `<div class="info-row"><span class="info-label">${k}</span><span class="info-value">${v}</span></div>`
        ).join('');
    } catch (e) { console.warn('系统信息失败:', e); }

    // 插件——卡片式展示
    try {
        const data = await fetch(`${API_BASE}/system/plugins`).then(r => r.json());
        const plugins = data.plugins || [];
        if (plugins.length === 0) {
            document.getElementById('pluginsList').innerHTML = '<p style="color:var(--text-secondary)">未安装插件</p>';
        } else {
            const html = '<div class="tools-grid">' + plugins.map(p => {
                const statusBadge = p.enabled ? '<span class="tool-badge parallel">启用</span>' : '<span class="tool-badge readonly">禁用</span>';
                return `<div class="tool-card">
                    <div class="tool-header">
                        <span class="tool-icon">🧩</span>
                        <span class="tool-name">${escapeHtml(p.name)}</span>
                    </div>
                    <div class="tool-desc">${escapeHtml(p.description || '暂无描述')}</div>
                    <div class="tool-meta">
                        ${p.version ? `<span class="tool-category">v${escapeHtml(p.version)}</span>` : ''}
                        ${p.author ? `<span class="tool-timeout">${escapeHtml(p.author)}</span>` : ''}
                        ${statusBadge}
                    </div>
                </div>`;
            }).join('') + '</div>';
            document.getElementById('pluginsList').innerHTML = html;
        }
    } catch (e) { console.warn('插件加载失败:', e); }

    // 人格
    try {
        const data = await fetch(`${API_BASE}/system/persona`).then(r => r.json());
        document.getElementById('personaEditor').value = data.prompt || '';
        // 显示来源信息
        const sourceEl = document.getElementById('personaSource');
        if (sourceEl) sourceEl.textContent = data.source ? `来源: ${data.source}` : '';
    } catch (e) { console.warn('人格加载失败:', e); }

    // 图片缓存状态
    try {
        const cacheData = await fetch(`${API_BASE}/messages/image-cache/stats`).then(r => r.json());
        const cacheEl = document.getElementById('imageCacheStatus');
        if (cacheEl) {
            if (cacheData.exists) {
                cacheEl.innerHTML = `📦 ${cacheData.count} 张图片 | ${cacheData.size_mb} MB`;
                cacheEl.style.color = 'var(--success)';
            } else {
                cacheEl.innerHTML = '📂 缓存目录未创建（首次收到图片时自动创建）';
                cacheEl.style.color = 'var(--text-secondary)';
            }
        }
    } catch (e) {
        const cacheEl = document.getElementById('imageCacheStatus');
        if (cacheEl) cacheEl.innerHTML = '无法获取缓存状态';
    }

    // Review 周期
    try {
        const flConfig = await fetch(`${API_BASE}/models/flashlite`).then(r => r.json());
        const el = document.getElementById('reviewIntervalHours');
        if (el) el.value = flConfig.review_interval_hours || 24;
    } catch (e) { console.warn('Review 周期加载失败:', e); }

    // 调试开关
    try {
        const debugConfig = await fetch(`${API_BASE}/models/debug-settings`).then(r => r.json());
        const toggle = document.getElementById('showToolUseToggle');
        if (toggle) toggle.checked = !!debugConfig.show_tool_use_status;
    } catch (e) { console.warn('调试设置加载失败:', e); }

    // 分段回复设置
    try {
        const segConfig = await fetch(`${API_BASE}/models/segmented-reply`).then(r => r.json());
        const methodEl = document.getElementById('segIntervalMethod');
        const mergeEl = document.getElementById('segMergeThreshold');
        const cleanupEl = document.getElementById('segCleanupRule');
        if (methodEl) methodEl.value = segConfig.interval_method || 'adaptive';
        if (mergeEl) mergeEl.value = segConfig.merge_threshold ?? 80;
        if (cleanupEl) cleanupEl.value = segConfig.content_cleanup_rule || '';
        // 延迟档位
        const dsEl = document.getElementById('segDelayShort');
        const dmEl = document.getElementById('segDelayMedium');
        const dlEl = document.getElementById('segDelayLong');
        if (dsEl) dsEl.value = segConfig.delay_short || '0.8,1.5';
        if (dmEl) dmEl.value = segConfig.delay_medium || '1.5,3.0';
        if (dlEl) dlEl.value = segConfig.delay_long || '2.5,4.5';
        // 最大分段数
        const msEl = document.getElementById('segMaxSegments');
        if (msEl) msEl.value = segConfig.max_segments ?? 3;
    } catch (e) { console.warn('分段设置加载失败:', e); }

    // 表情包管理
    try {
        const segConfig = await fetch(`${API_BASE}/models/segmented-reply`).then(r => r.json());
        const easEl = document.getElementById('emojiSendAfterSegment');
        const epEl2 = document.getElementById('emojiProbability');
        if (easEl) easEl.value = segConfig.emoji_send_after_segment ?? 1;
        if (epEl2) epEl2.value = segConfig.emoji_probability ?? 0.7;
    } catch (e) { console.warn('表情包设置加载失败:', e); }
    loadEmojiGrid();

    // 消息持久化策略
    try {
        const policy = await fetch(`${API_BASE}/models/storage-policy`).then(r => r.json());
        const hotEl = document.getElementById('storageHotDays');
        const coldEl = document.getElementById('storageColdDays');
        const archiveEl = document.getElementById('storageArchiveDays');
        if (hotEl) hotEl.value = policy.hot_days || 7;
        if (coldEl) coldEl.value = policy.cold_days || 30;
        if (archiveEl) archiveEl.value = policy.archive_days || 90;
    } catch (e) { console.warn('持久化策略加载失败:', e); }

    // CHECKPOINT 策略
    try {
        const flConfig = await fetch(`${API_BASE}/models/flashlite`).then(r => r.json());
        const cpTL = document.getElementById('cpTokenLimit');
        const cpKR = document.getElementById('cpKeepRecent');
        const cpFR = document.getElementById('cpCompressFrontRatio');
        const cpCD = document.getElementById('cpCooldownSeconds');
        const cpTMin = document.getElementById('cpTargetMin');
        const cpTMax = document.getElementById('cpTargetMax');
        if (cpTL) cpTL.value = flConfig.checkpoint_limit || 50000;
        if (cpKR) cpKR.value = flConfig.checkpoint_keep_recent || 10;
        if (cpFR) cpFR.value = flConfig.checkpoint_compress_front_ratio || 0.7;
        if (cpCD) cpCD.value = flConfig.checkpoint_cooldown_seconds || 300;
        if (cpTMin) cpTMin.value = flConfig.checkpoint_target_min || 0.20;
        if (cpTMax) cpTMax.value = flConfig.checkpoint_target_max || 0.40;
    } catch (e) { console.warn('CHECKPOINT 策略加载失败:', e); }
}

async function saveCheckpointStrategy() {
    const el = document.getElementById('cpStrategySaveResult');
    try {
        const body = {
            checkpoint_limit: parseInt(document.getElementById('cpTokenLimit').value) || 50000,
            checkpoint_keep_recent: parseInt(document.getElementById('cpKeepRecent').value) || 10,
            checkpoint_compress_front_ratio: parseFloat(document.getElementById('cpCompressFrontRatio').value) || 0.7,
            checkpoint_cooldown_seconds: parseInt(document.getElementById('cpCooldownSeconds').value) || 300,
            checkpoint_target_min: parseFloat(document.getElementById('cpTargetMin').value) || 0.20,
            checkpoint_target_max: parseFloat(document.getElementById('cpTargetMax').value) || 0.40,
        };
        const res = await fetch(`${API_BASE}/models/flashlite`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(x=>x.json());
        el.textContent = res.success ? '✅ 已保存' : `❌ ${res.detail||'失败'}`;
        el.style.color = res.success ? 'var(--success)' : 'var(--danger)';
    } catch (e) { el.textContent = `❌ ${e.message}`; el.style.color = 'var(--danger)'; }
}
async function saveStoragePolicy() {
    const el = document.getElementById('storagePolicySaveResult');
    try {
        const body = {
            hot_days: parseInt(document.getElementById('storageHotDays').value) || 7,
            cold_days: parseInt(document.getElementById('storageColdDays').value) || 30,
            archive_days: parseInt(document.getElementById('storageArchiveDays').value) || 90,
        };
        const res = await fetch(`${API_BASE}/models/storage-policy`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.success) {
            el.innerHTML = '<span style="color:var(--success)">✅ 已保存</span>';
            // 回填可能被约束调整的值
            if (data.policy) {
                document.getElementById('storageHotDays').value = data.policy.hot_days;
                document.getElementById('storageColdDays').value = data.policy.cold_days;
                document.getElementById('storageArchiveDays').value = data.policy.archive_days;
            }
        } else {
            el.innerHTML = '<span style="color:var(--error)">❌ 保存失败</span>';
        }
    } catch (e) {
        el.innerHTML = `<span style="color:var(--error)">❌ ${e.message}</span>`;
    }
    setTimeout(() => { if (el) el.innerHTML = ''; }, 3000);
}

async function saveSegmentedReply() {
    const el = document.getElementById('segSaveResult');
    try {
        const body = {
            interval_method: document.getElementById('segIntervalMethod').value,
            merge_threshold: parseInt(document.getElementById('segMergeThreshold').value) || 80,
            content_cleanup_rule: document.getElementById('segCleanupRule').value,
            delay_short: document.getElementById('segDelayShort').value,
            delay_medium: document.getElementById('segDelayMedium').value,
            delay_long: document.getElementById('segDelayLong').value,
            max_segments: parseInt(document.getElementById('segMaxSegments').value) ?? 3,
        };
        const res = await fetch(`${API_BASE}/models/segmented-reply`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        }).then(r => r.json());
        el.textContent = res.success ? '✅ 已保存（重启 AstrBot 后生效）' : `❌ ${res.error}`;
        el.style.color = res.success ? 'var(--success)' : 'var(--danger)';
        setTimeout(() => { el.textContent = ''; }, 4000);
    } catch (e) { el.textContent = `❌ ${e.message}`; el.style.color = 'var(--danger)'; }
}

async function loadEmojiGrid() {
    const grid = document.getElementById('emojiGrid');
    const countEl = document.getElementById('emojiCount');
    if (!grid) return;
    try {
        const data = await fetch(`${API_BASE}/models/emojis`).then(r => r.json());
        const emojis = data.emojis || [];
        if (countEl) countEl.textContent = `共 ${emojis.length} 个`;
        if (!emojis.length) { grid.innerHTML = '<p style="color:var(--text-secondary);grid-column:1/-1">暂无表情包</p>'; return; }
        grid.innerHTML = emojis.map(e => {
            // 内容 tag
            const contentTags = (e.content_tags || []).map(kw =>
                `<span style="display:inline-block;padding:2px 7px;border-radius:8px;font-size:11px;background:rgba(52,152,219,0.15);color:#5dade2;margin:2px">${escapeHtml(kw)}</span>`
            ).join('');
            // 通用 tag
            const universalTag = e.is_universal
                ? '<span style="display:inline-block;padding:2px 7px;border-radius:8px;font-size:11px;background:rgba(155,89,182,0.2);color:#bb8fce;margin:2px">通用</span>'
                : '';
            return `<div style="background:var(--surface);border-radius:12px;padding:8px;position:relative;border:1px solid var(--border)">
                <img src="${API_BASE}/models/emojis/image/${encodeURIComponent(e.name)}" alt="${escapeHtml(e.name)}" style="width:100%;aspect-ratio:1;object-fit:contain;border-radius:8px;background:var(--surface-hover)" onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 80 80%22><text x=%2240%22 y=%2245%22 text-anchor=%22middle%22 font-size=%2230%22>🎭</text></svg>'">
                <div style="margin-top:6px;min-height:28px">${contentTags}${universalTag}</div>
                <div style="display:flex;gap:4px;margin-top:4px">
                    <button onclick="editEmojiKeywords('${escapeHtml(e.name)}')" style="flex:1;padding:3px;border:none;border-radius:6px;background:var(--surface-hover);color:var(--text-secondary);cursor:pointer;font-size:11px" title="编辑关键词">✏️ 编辑</button>
                    <button onclick="deleteEmoji('${escapeHtml(e.name)}')" style="padding:3px 8px;border:none;border-radius:6px;background:rgba(231,76,60,0.1);color:#e74c3c;cursor:pointer;font-size:11px" title="删除">🗑</button>
                </div>
            </div>`;
        }).join('');
    } catch (e) {
        grid.innerHTML = `<p style="color:var(--danger);grid-column:1/-1">加载失败: ${e.message}</p>`;
    }
}

async function saveEmojiSettings() {
    const el = document.getElementById('emojiSettingsSaveResult');
    try {
        const body = {
            emoji_send_after_segment: parseInt(document.getElementById('emojiSendAfterSegment').value) || 1,
            emoji_probability: parseFloat(document.getElementById('emojiProbability').value) ?? 0.7,
        };
        const res = await fetch(`${API_BASE}/models/segmented-reply`, {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
        }).then(r => r.json());
        el.textContent = res.success ? '✅ 已保存' : `❌ ${res.error}`;
        el.style.color = res.success ? 'var(--success)' : 'var(--danger)';
        setTimeout(() => { el.textContent = ''; }, 3000);
    } catch (e) { el.textContent = `❌ ${e.message}`; el.style.color = 'var(--danger)'; }
}

async function editEmojiKeywords(filename) {
    const newKw = prompt('请输入新的关键词（空格分隔）:');
    if (newKw === null) return;
    const keywords = newKw.split(/\s+/).filter(k => k);
    if (!keywords.length) { alert('关键词不能为空'); return; }
    try {
        const res = await fetch(`${API_BASE}/models/emojis/update-keywords`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ old_name: filename, new_keywords: keywords }),
        }).then(r => r.json());
        if (res.success) loadEmojiGrid();
        else alert(res.detail || '更新失败');
    } catch (e) { alert('更新失败: ' + e.message); }
}

async function deleteEmoji(filename) {
    if (!confirm(`确定删除 ${filename}？`)) return;
    try {
        const res = await fetch(`${API_BASE}/models/emojis/${encodeURIComponent(filename)}`, { method: 'DELETE' }).then(r => r.json());
        if (res.success) loadEmojiGrid();
        else alert(res.detail || '删除失败');
    } catch (e) { alert('删除失败: ' + e.message); }
}

async function uploadEmojis(files) {
    if (!files || !files.length) return;
    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    try {
        const res = await fetch(`${API_BASE}/models/emojis/upload`, {
            method: 'POST', body: fd,
        }).then(r => r.json());
        if (res.success) {
            loadEmojiGrid();
        } else {
            alert(res.detail || '上传失败');
        }
    } catch (e) { alert('上传失败: ' + e.message); }
    document.getElementById('emojiUploadInput').value = '';
}

async function savePersona() {
    const prompt = document.getElementById('personaEditor').value;
    try {
        const res = await fetch(`${API_BASE}/system/persona`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({prompt}),
        }).then(r => r.json());
        document.getElementById('personaSaveResult').textContent = res.success ? '✅ 已保存' : `❌ ${res.error}`;
        setTimeout(() => document.getElementById('personaSaveResult').textContent = '', 3000);
    } catch (e) { document.getElementById('personaSaveResult').textContent = `❌ ${e.message}`; }
}

async function saveReviewInterval() {
    const val = parseInt(document.getElementById('reviewIntervalHours').value);
    const el = document.getElementById('reviewIntervalResult');
    if (isNaN(val) || val < 1 || val > 168) {
        el.textContent = '❌ 范围 1-168 小时'; el.style.color = 'var(--danger)';
        return;
    }
    try {
        const res = await fetch(`${API_BASE}/models/flashlite`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({review_interval_hours: val}),
        }).then(r => r.json());
        el.textContent = res.success ? `✅ 已保存: ${val}h` : `❌ ${res.error}`;
        el.style.color = res.success ? 'var(--success)' : 'var(--danger)';
        setTimeout(() => { el.textContent = ''; }, 3000);
    } catch (e) { el.textContent = `❌ ${e.message}`; el.style.color = 'var(--danger)'; }
}

async function toggleToolDebug() {
    const checked = document.getElementById('showToolUseToggle').checked;
    const el = document.getElementById('toolDebugResult');
    try {
        const res = await fetch(`${API_BASE}/models/debug-settings`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({show_tool_use_status: checked}),
        }).then(r => r.json());
        el.textContent = res.success ? `✅ 已${checked ? '开启' : '关闭'}（重启 AstrBot 后生效）` : `❌ ${res.error}`;
        el.style.color = res.success ? 'var(--success)' : 'var(--danger)';
        setTimeout(() => { el.textContent = ''; }, 4000);
    } catch (e) { el.textContent = `❌ ${e.message}`; el.style.color = 'var(--danger)'; }
}

async function exportData() {
    document.getElementById('exportResult').textContent = '导出中...';
    try {
        const data = await fetch(`${API_BASE}/system/export`, {method:'POST'}).then(r => r.json());
        document.getElementById('exportResult').textContent = data.success
            ? `✅ 导出完成: ${data.path} (${data.size_mb} MB)`
            : `❌ ${data.error}`;
    } catch (e) { document.getElementById('exportResult').textContent = `❌ ${e.message}`; }
}

async function importData() {
    const fileInput = document.getElementById('importFile');
    const resultEl = document.getElementById('importResult');
    if (!fileInput.files.length) {
        resultEl.textContent = '❌ 请先选择一个 .zip 导出包';
        resultEl.style.color = 'var(--danger)';
        return;
    }
    resultEl.textContent = '导入中...';
    resultEl.style.color = 'var(--text-secondary)';
    try {
        const formData = new FormData();
        formData.append('file', fileInput.files[0]);
        const res = await fetch(`${API_BASE}/system/import`, {
            method: 'POST',
            body: formData,
        }).then(r => r.json());
        if (res.success) {
            resultEl.textContent = `✅ ${res.message}`;
            resultEl.style.color = 'var(--success)';
        } else {
            resultEl.textContent = `❌ ${res.error}`;
            resultEl.style.color = 'var(--danger)';
        }
    } catch (e) {
        resultEl.textContent = `❌ ${e.message}`;
        resultEl.style.color = 'var(--danger)';
    }
}

async function loadLogs() {
    try {
        const data = await fetch(`${API_BASE}/system/logs`).then(r => r.json());
        document.getElementById('logContent').textContent = (data.logs || []).join('');
    } catch (e) { document.getElementById('logContent').textContent = `加载失败: ${e.message}`; }
}

async function saveConsolePassword() {
    const pw = document.getElementById('consolePassword').value;
    const pwConfirm = document.getElementById('consolePasswordConfirm').value;
    const r = document.getElementById('passwordSaveResult');
    if (pw !== pwConfirm) {
        r.textContent = '❌ 两次输入的密码不一致';
        r.style.color = 'var(--danger)';
        return;
    }
    try {
        const res = await fetch(`${API_BASE}/system/password`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({password: pw}),
        }).then(x => x.json());
        r.textContent = res.success ? (pw ? '✅ 密码已设置，重启后生效' : '✅ 已清除密码保护') : `❌ ${res.error}`;
        r.style.color = res.success ? 'var(--success)' : 'var(--danger)';
        document.getElementById('consolePassword').value = '';
        document.getElementById('consolePasswordConfirm').value = '';
    } catch (e) { r.textContent = `❌ ${e.message}`; r.style.color = 'var(--danger)'; }
}



// ============================================================
// 成本监控
// ============================================================

async function loadCostPage() {
    const period = document.getElementById('costPeriod')?.value || 'week';

    // 1. 概览
    try {
        const data = await fetch(`${API_BASE}/cost/summary?period=${period}`).then(r => r.json());
        document.getElementById('costTotalCny').textContent = `¥${data.total_cost_cny || 0}`;
        document.getElementById('costTotalCalls').textContent = (data.total_calls || 0).toLocaleString();
        document.getElementById('costCacheRate').textContent = `${data.cache_hit_rate || 0}%`;
        document.getElementById('costFlashliteCalls').textContent = (data.flashlite_calls || 0).toLocaleString();
        document.getElementById('costUsdDetail').textContent = `$${data.total_cost_usd || 0} USD`;
        document.getElementById('costPromptTokens').textContent = (data.total_prompt_tokens || 0).toLocaleString();
        document.getElementById('costCachedTokens').textContent = (data.total_cached_tokens || 0).toLocaleString();
        document.getElementById('costOutputTokens').textContent = (data.total_output_tokens || 0).toLocaleString();
        const storageFee = data.storage_cost_usd || 0;
        document.getElementById('costStorageFee').textContent = storageFee > 0 ? `$${storageFee.toFixed(6)}` : '$0';
    } catch (e) {
        console.warn('成本概览加载失败:', e);
    }

    // 2. 按模型
    try {
        const data = await fetch(`${API_BASE}/cost/by-model?period=${period}`).then(r => r.json());
        const models = data.models || [];
        const el = document.getElementById('costByModel');
        if (models.length === 0) {
            el.innerHTML = '<p style="color:var(--text-secondary);text-align:center;padding:16px 0">暂无数据</p>';
        } else {
            el.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:13px">
                <tr style="color:var(--text-secondary);border-bottom:1px solid var(--border)">
                    <th style="text-align:left;padding:6px 8px">模型</th>
                    <th style="text-align:right;padding:6px 8px">调用</th>
                    <th style="text-align:right;padding:6px 8px">Cache%</th>
                    <th style="text-align:right;padding:6px 8px">费用(¥)</th>
                </tr>
                ${models.map(m => `<tr style="border-bottom:1px solid rgba(107,92,245,0.08)">
                    <td style="padding:6px 8px;color:var(--text-primary)">${escapeHtml(m.model)}</td>
                    <td style="padding:6px 8px;text-align:right;color:var(--text-secondary)">${m.calls}</td>
                    <td style="padding:6px 8px;text-align:right;color:var(--success)">${m.cache_hit_rate}%</td>
                    <td style="padding:6px 8px;text-align:right;color:var(--accent)">¥${m.cost_cny}</td>
                </tr>`).join('')}
            </table>`;
        }
    } catch (e) {
        document.getElementById('costByModel').innerHTML = '<p style="color:var(--danger)">模型数据加载失败</p>';
    }

    // 3. 按窗口
    try {
        const data = await fetch(`${API_BASE}/cost/by-window?period=${period}`).then(r => r.json());
        const windows = data.windows || [];
        const el = document.getElementById('costByWindow');
        if (windows.length === 0) {
            el.innerHTML = '<p style="color:var(--text-secondary);text-align:center;padding:16px 0">暂无数据</p>';
        } else {
            el.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:13px">
                <tr style="color:var(--text-secondary);border-bottom:1px solid var(--border)">
                    <th style="text-align:left;padding:6px 8px">窗口</th>
                    <th style="text-align:right;padding:6px 8px">FL</th>
                    <th style="text-align:right;padding:6px 8px">主模型</th>
                    <th style="text-align:right;padding:6px 8px">工具</th>
                    <th style="text-align:right;padding:6px 8px">Tokens</th>
                    <th style="text-align:right;padding:6px 8px">费用(¥)</th>
                </tr>
                ${windows.map(w => {
                    const wkName = w.window_key.replace('GroupMessage:', '群').replace('FriendMessage:', '私聊').replace('PrivateMessage:', '私聊');
                    return `<tr style="border-bottom:1px solid rgba(107,92,245,0.08)">
                        <td style="padding:6px 8px;color:var(--text-primary);max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(w.window_key)}">${escapeHtml(wkName)}</td>
                        <td style="padding:6px 8px;text-align:right;color:var(--success)">${w.flashlite_calls}</td>
                        <td style="padding:6px 8px;text-align:right;color:var(--warning)">${w.main_calls}</td>
                        <td style="padding:6px 8px;text-align:right;color:var(--text-secondary)">${w.tool_calls}</td>
                        <td style="padding:6px 8px;text-align:right;color:var(--text-secondary)">${w.total_tokens.toLocaleString()}</td>
                        <td style="padding:6px 8px;text-align:right;color:var(--accent)">¥${w.cost_cny}</td>
                    </tr>`;
                }).join('')}
            </table>`;
        }
    } catch (e) {
        document.getElementById('costByWindow').innerHTML = '<p style="color:var(--danger)">窗口数据加载失败</p>';
    }

    // 4. 时间轴 Chart.js
    try {
        // 沿用用户已选粒度（如有），否则按 period 推断默认值
        const gran = window._costTimelineGran || (period === 'today' ? 'hour' : 'day');
        window._costTimelineGran = gran;
        const data = await fetch(`${API_BASE}/cost/timeline?period=${period}&granularity=${gran}`).then(r => r.json());
        window._costTimelineData = data.timeline || [];
        window._costTimelineGran = gran;
        renderTimelineChart();
    } catch (e) {
        console.warn('趋势数据加载失败:', e);
    }

    // 缓存模型/窗口数据供饼图用
    try {
        const mData = await fetch(`${API_BASE}/cost/by-model?period=${period}`).then(r => r.json());
        window._costModelData = mData.models || [];
    } catch(e) {}
    try {
        const wData = await fetch(`${API_BASE}/cost/by-window?period=${period}`).then(r => r.json());
        window._costWindowData = wData.windows || [];
    } catch(e) {}

    // 默认显示时间维度
    switchChartDimension('time');
    // 初始粒度高亮
    ['granHour','granDay'].forEach(id => { const el = document.getElementById(id); if (el) el.style.opacity = '0.5'; });
    const initGranEl = document.getElementById(period === 'today' ? 'granHour' : 'granDay');
    if (initGranEl) initGranEl.style.opacity = '1';

    // 5. 加载采样配置
    loadSamplingConfig();

    // R4: 自动刷新（30s）
    if (window._costRefreshTimer) clearInterval(window._costRefreshTimer);
    window._costRefreshTimer = setInterval(() => {
        const currentPage = window.location.hash.slice(1) || 'dashboard';
        if (currentPage === 'cost') {
            loadCostPage();
        } else {
            clearInterval(window._costRefreshTimer);
            window._costRefreshTimer = null;
        }
    }, 30000);
}

// ============================================================
// R6: Chart.js 图表渲染
// ============================================================

// 图表全局配置
if (typeof Chart !== 'undefined') {
    Chart.defaults.color = '#a0a0b8';
    Chart.defaults.borderColor = 'rgba(107,92,245,0.12)';
    Chart.defaults.font.family = "'Inter','Noto Sans SC',sans-serif";
}

window._chartTimeline = null;
window._chartPie = null;
const CHART_COLORS = ['#6b5cf5','#4ecdc4','#ff6b6b','#ffd93d','#6c5ce7','#a8e6cf','#ff8a5c','#81ecec','#fd79a8','#636e72'];

function renderTimelineChart() {
    const timeline = window._costTimelineData || [];
    const gran = window._costTimelineGran || 'day';
    const ctx = document.getElementById('costChartTimeline');
    if (!ctx) return;

    if (window._chartTimeline) { window._chartTimeline.destroy(); window._chartTimeline = null; }

    if (timeline.length === 0) {
        ctx.parentElement.innerHTML = '<p style="color:var(--text-secondary);text-align:center;padding:60px 0">暂无趋势数据</p>';
        return;
    }

    const labels = timeline.map(t => gran === 'hour' ? t.time.slice(11,13) + ':00' : t.time.slice(5));
    window._chartTimeline = new Chart(ctx, {
        type: 'line',
        data: {
            labels,
            datasets: [{
                label: '调用次数',
                data: timeline.map(t => t.calls),
                borderColor: '#6b5cf5',
                backgroundColor: 'rgba(107,92,245,0.15)',
                fill: true,
                tension: 0.35,
                pointRadius: 3,
                pointHoverRadius: 6,
                yAxisID: 'y',
            }, {
                label: '费用 ($)',
                data: timeline.map(t => parseFloat(t.cost_usd) || 0),
                borderColor: '#4ecdc4',
                backgroundColor: 'rgba(78,205,196,0.1)',
                fill: false,
                tension: 0.35,
                pointRadius: 2,
                borderDash: [4,4],
                yAxisID: 'y1',
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { position: 'top', labels: { boxWidth: 12, padding: 16 } },
                tooltip: { backgroundColor: 'rgba(18,18,30,0.95)', cornerRadius: 8, padding: 10 }
            },
            scales: {
                y: { beginAtZero: true, title: { display: true, text: '调用次数' }, grid: { color: 'rgba(107,92,245,0.06)' } },
                y1: { beginAtZero: true, position: 'right', title: { display: true, text: '费用 ($)' }, grid: { drawOnChartArea: false } },
                x: { grid: { color: 'rgba(107,92,245,0.06)' } }
            }
        }
    });
}

function renderPieChart(dimension) {
    const ctx = document.getElementById('costChartPie');
    if (!ctx) return;

    if (window._chartPie) { window._chartPie.destroy(); window._chartPie = null; }

    let labels, dataValues, title;
    if (dimension === 'model') {
        const models = window._costModelData || [];
        if (models.length === 0) { ctx.parentElement.innerHTML = '<p style="color:var(--text-secondary);text-align:center;padding:60px 0">暂无模型数据</p>'; return; }
        labels = models.map(m => m.model);
        dataValues = models.map(m => m.calls);
        title = '按模型分布';
    } else {
        const windows = window._costWindowData || [];
        if (windows.length === 0) { ctx.parentElement.innerHTML = '<p style="color:var(--text-secondary);text-align:center;padding:60px 0">暂无窗口数据</p>'; return; }
        labels = windows.map(w => w.window_key.replace('GroupMessage:', '群').replace('FriendMessage:', '私聊'));
        dataValues = windows.map(w => w.flashlite_calls + w.main_calls + w.tool_calls);
        title = '按窗口分布';
    }

    window._chartPie = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels,
            datasets: [{
                data: dataValues,
                backgroundColor: CHART_COLORS.slice(0, labels.length),
                borderWidth: 2,
                borderColor: '#12121e',
                hoverOffset: 8,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { position: 'right', labels: { boxWidth: 12, padding: 10, font: { size: 12 } } },
                title: { display: true, text: title, font: { size: 14 }, padding: { bottom: 10 } },
                tooltip: { backgroundColor: 'rgba(18,18,30,0.95)', cornerRadius: 8 }
            }
        }
    });
}

function switchChartDimension(dim) {
    // Tab 高亮
    ['tabTime','tabModel','tabWindow'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.opacity = '0.5';
    });
    const activeTab = dim === 'time' ? 'tabTime' : dim === 'model' ? 'tabModel' : 'tabWindow';
    const activeEl = document.getElementById(activeTab);
    if (activeEl) activeEl.style.opacity = '1';

    const timeWrap = document.getElementById('chartTimelineWrap');
    const pieWrap = document.getElementById('chartPieWrap');

    if (dim === 'time') {
        if (timeWrap) timeWrap.style.display = 'block';
        if (pieWrap) pieWrap.style.display = 'none';
        renderTimelineChart();
    } else {
        if (timeWrap) timeWrap.style.display = 'none';
        if (pieWrap) { pieWrap.style.display = 'block'; pieWrap.innerHTML = '<canvas id="costChartPie"></canvas>'; }
        renderPieChart(dim);
    }
}

async function switchChartGranularity(gran) {
    // 高亮粒度按钮
    ['granHour','granDay'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.opacity = '0.5';
    });
    const activeGran = gran === 'hour' ? 'granHour' : 'granDay';
    const activeEl = document.getElementById(activeGran);
    if (activeEl) activeEl.style.opacity = '1';

    // 重新加载 timeline 数据
    const period = document.getElementById('costPeriodSelect')?.value || 'week';
    try {
        const data = await fetch(`${API_BASE}/cost/timeline?period=${period}&granularity=${gran}`).then(r => r.json());
        window._costTimelineData = data.timeline || [];
        window._costTimelineGran = gran;
        renderTimelineChart();
    } catch (e) {
        console.warn('粒度切换加载失败:', e);
    }
}

// 采样策略动态显示
function toggleSamplingOptions() {
    const mode = document.getElementById('samplingMode')?.value;
    const dynOpts = document.getElementById('dynamicSamplingOptions');
    if (dynOpts) dynOpts.style.display = mode === 'dynamic' ? 'block' : 'none';
}

// 加载采样配置
async function loadSamplingConfig() {
    try {
        const data = await fetch(`${API_BASE}/models/flashlite`).then(r => r.json());
        // 采样模式
        const modeEl = document.getElementById('samplingMode');
        if (modeEl && data.sampling_mode) modeEl.value = data.sampling_mode;
        // 固定间隔（即 sync_interval）
        const fixedEl = document.getElementById('samplingFixedInterval');
        if (fixedEl && data.sync_interval) fixedEl.value = data.sync_interval;
        // 最少消息数
        const minEl = document.getElementById('samplingMinMsgs');
        if (minEl && data.sync_time_min_msgs !== undefined) minEl.value = data.sync_time_min_msgs;
        // 时间兜底秒数
        const timeEl = document.getElementById('samplingTimeInterval');
        if (timeEl && data.sync_time_interval !== undefined) timeEl.value = data.sync_time_interval;
        // 动态采样子参数
        if (data.dynamic_sampling) {
            const ds = data.dynamic_sampling;
            const winEl = document.getElementById('samplingWindowMin');
            if (winEl && ds.window_minutes) winEl.value = ds.window_minutes;
            const thrEl = document.getElementById('samplingThresholds');
            if (thrEl && ds.thresholds) thrEl.value = ds.thresholds.join(',');
            const intEl = document.getElementById('samplingIntervals');
            if (intEl && ds.intervals) intEl.value = ds.intervals.join(',');
        }
        toggleSamplingOptions();
        // R5: 群聊独立配置
        if (data.group_overrides && typeof data.group_overrides === 'object') {
            window._groupOverrides = data.group_overrides;
        }
        renderGroupOverrides();
        loadKnownGroups();
    } catch (e) {
        console.warn('采样配置加载失败:', e);
    }
}

// 保存采样配置（写入 FlashLite 的 _conf_schema.json）
async function saveSamplingConfig() {
    const r = document.getElementById('samplingSaveResult');
    try {
        const body = {
            sync_interval: parseInt(document.getElementById('samplingFixedInterval')?.value) || 5,
            sampling_mode: document.getElementById('samplingMode')?.value || 'fixed',
            sync_time_min_msgs: parseInt(document.getElementById('samplingMinMsgs')?.value) || 2,
            sync_time_interval: parseInt(document.getElementById('samplingTimeInterval')?.value) || 60,
        };
        // 动态采样子参数
        if (body.sampling_mode === 'dynamic') {
            body.dynamic_sampling = {
                window_minutes: parseInt(document.getElementById('samplingWindowMin')?.value) || 10,
                thresholds: (document.getElementById('samplingThresholds')?.value || '5,15,30').split(',').map(x => parseInt(x.trim())).filter(x => !isNaN(x)),
                intervals: (document.getElementById('samplingIntervals')?.value || '3,5,10,15').split(',').map(x => parseInt(x.trim())).filter(x => !isNaN(x)),
            };
        }
        const res = await fetch(`${API_BASE}/models/flashlite`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        }).then(x => x.json());
        r.textContent = res.success ? '✅ 已保存（重启 AstrBot 生效）' : `❌ ${res.detail || '保存失败'}`;
        r.style.color = res.success ? 'var(--success)' : 'var(--danger)';
    } catch (e) {
        r.textContent = `❌ ${e.message}`;
        r.style.color = 'var(--danger)';
    }
}

// R5: 群聊独立配置管理
window._groupOverrides = {};

function renderGroupOverrides() {
    const container = document.getElementById('groupOverridesTable');
    if (!container) return;
    const entries = Object.entries(window._groupOverrides);
    if (entries.length === 0) {
        container.innerHTML = '<p style="color:var(--text-secondary);font-size:12px;padding:8px 0">暂无独立配置，所有群使用全局设置</p>';
        return;
    }
    const _tp = {full:'全部',search_only:'仅搜索',none:'禁用'};
    let html = '<table style="width:100%;border-collapse:collapse;font-size:12px">';
    html += '<tr style="color:var(--text-secondary);border-bottom:1px solid var(--border)">'
        + '<th style="text-align:left;padding:5px 6px">群号</th>'
        + '<th style="text-align:center;padding:5px 6px">间隔</th>'
        + '<th style="text-align:center;padding:5px 6px">回复上限</th>'
        + '<th style="text-align:center;padding:5px 6px">工具</th>'
        + '<th style="text-align:center;padding:5px 6px">思考</th>'
        + '<th style="text-align:center;padding:5px 6px">状态</th>'
        + '<th style="text-align:right;padding:5px 6px">操作</th></tr>';
    for (const [gid, cfg] of entries) {
        const sg = escapeHtml(gid);
        const en = cfg.enabled !== false;
        html += `<tr style="border-bottom:1px solid rgba(107,92,245,0.08)">
            <td style="padding:5px 6px;color:var(--text-primary)">${sg}</td>
            <td style="padding:5px 6px;text-align:center;color:var(--accent)">${cfg.sync_interval||'全局'}</td>
            <td style="padding:5px 6px;text-align:center">${cfg.reply_length_limit||'不限'}</td>
            <td style="padding:5px 6px;text-align:center">${_tp[cfg.tool_permission]||'全部'}</td>
            <td style="padding:5px 6px;text-align:center">${cfg.main_thinking_budget||'默认'}</td>
            <td style="padding:5px 6px;text-align:center">${en?'<span style="color:var(--success)">✅</span>':'<span style="color:var(--text-secondary)">⏸️</span>'}</td>
            <td style="padding:5px 6px;text-align:right">
                <button class="group-toggle-btn" data-gid="${sg}" style="background:none;border:none;cursor:pointer;font-size:13px;padding:2px 4px" title="${en?'禁用':'启用'}">${en?'⏸️':'▶️'}</button>
                <button class="group-remove-btn" data-gid="${sg}" style="background:none;border:none;cursor:pointer;font-size:13px;padding:2px 4px;color:var(--danger)" title="删除">🗑️</button>
            </td>
        </tr>`;
    }
    html += '</table>';
    container.innerHTML = html;
    container.querySelectorAll('.group-toggle-btn').forEach(btn => {
        btn.addEventListener('click', () => toggleGroupOverride(btn.dataset.gid));
    });
    container.querySelectorAll('.group-remove-btn').forEach(btn => {
        btn.addEventListener('click', () => removeGroupOverride(btn.dataset.gid));
    });
}

function addGroupOverride() {
    const gid = document.getElementById('groupOverrideId')?.value?.trim();
    if (!gid) { alert('请输入群号'); return; }
    if (!/^\d+$/.test(gid)) { alert('群号必须是纯数字'); return; }
    const entry = {
        sync_interval: parseInt(document.getElementById('groupOverrideInterval')?.value) || 5,
        enabled: true,
    };
    const rl = document.getElementById('groupOverrideReplyLimit')?.value;
    if (rl) entry.reply_length_limit = parseInt(rl);
    const tp = document.getElementById('groupOverrideToolPerm')?.value;
    if (tp && tp !== 'full') entry.tool_permission = tp;
    const tb = document.getElementById('groupOverrideThinkBudget')?.value;
    if (tb) entry.main_thinking_budget = parseInt(tb);
    window._groupOverrides[gid] = entry;
    renderGroupOverrides();
    document.getElementById('groupOverrideId').value = '';
    saveGroupOverrides();
}

function removeGroupOverride(gid) {
    delete window._groupOverrides[gid];
    renderGroupOverrides();
    saveGroupOverrides();
}

function toggleGroupOverride(gid) {
    if (window._groupOverrides[gid]) {
        window._groupOverrides[gid].enabled = !window._groupOverrides[gid].enabled;
        renderGroupOverrides();
        saveGroupOverrides();
    }
}

async function saveGroupOverrides() {
    try {
        await fetch(`${API_BASE}/models/flashlite`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ group_overrides: window._groupOverrides }),
        });
    } catch (e) {
        console.warn('群聊独立配置保存失败:', e);
    }
}

// 加载已知群号到 datalist
async function loadKnownGroups() {
    try {
        const data = await fetch(`${API_BASE}/cost/known-groups`).then(r => r.json());
        const dl = document.getElementById('knownGroupsList');
        if (dl && data.groups) {
            dl.innerHTML = data.groups.map(g => `<option value="${escapeHtml(g)}">`).join('');
        }
    } catch (e) { /* 非关键功能，静默失败 */ }
}

// ============================================================
// 初始化
// ============================================================

const initialPage = window.location.hash.slice(1) || 'dashboard';
navigateTo(initialPage);

// 自动刷新
setInterval(updateTime, 1000);
setInterval(() => {
    const currentPage = document.querySelector('.nav-item.active')?.dataset?.page;
    if (currentPage === 'dashboard') loadDashboard();
}, 15000);
