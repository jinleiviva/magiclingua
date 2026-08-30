/**
 * Local Bridge
 *
 * 同步扩展配置到本地服务页面（http://localhost:18770/*）。
 * 让 popup 里的「PDF 对照阅读」开关能够控制 /pdf 页面的翻译模式。
 *
 * 实现：把 chrome.storage.sync 镜像到 localStorage，/pdf 页面在提交时读取。
 */

const STORAGE_KEY = "hy_mt_pdf_bilingual";

function mirrorToLocalStorage(value) {
    try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(!!value));
    } catch (e) {
        // 隐私模式或无 storage 可用时静默
    }
}

// 页面加载时立即同步一次
chrome.storage.sync.get({ pdfBilingual: false }, (cfg) => {
    mirrorToLocalStorage(cfg.pdfBilingual);
});

// 后续变更实时同步
chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "sync" || !changes.pdfBilingual) return;
    mirrorToLocalStorage(changes.pdfBilingual.newValue);
});
