/**
 * YouTube Player Bridge（运行在页面主世界 MAIN world）
 *
 * 内容脚本运行在隔离世界，看不到 window.ytInitialPlayerResponse 等
 * 页面级 JS 全局。这个桥接脚本运行在页面主世界，代替隔离世界读取
 * 播放器的字幕轨道信息，通过 CustomEvent 回传。
 */
(function () {
    'use strict';

    document.documentElement.addEventListener('hy-mt-need-tracks', () => {
        let detail = 'null';
        try {
            let pr = null;
            // movie_player.getPlayerResponse() 在 SPA 切换视频后依然返回新数据，
            // 比 ytInitialPlayerResponse 更可靠
            const player = document.getElementById('movie_player');
            if (player && typeof player.getPlayerResponse === 'function') {
                pr = player.getPlayerResponse();
            }
            if (!pr) pr = window.ytInitialPlayerResponse;
            const tracks = (pr && pr.captions && pr.captions.playerCaptionsTracklistRenderer
                && pr.captions.playerCaptionsTracklistRenderer.captionTracks) || null;
            const videoId = (pr && pr.videoDetails && pr.videoDetails.videoId) || null;
            detail = JSON.stringify({ tracks, videoId });
        } catch (e) {
            detail = 'null';
        }
        document.documentElement.dispatchEvent(
            new CustomEvent('hy-mt-tracks-response', { detail })
        );
    });
})();
