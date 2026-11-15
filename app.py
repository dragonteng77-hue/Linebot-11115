"""
臺南開放資料 ➜ LINE 投放 (Gradio + PWA + RWD)

流程:
① 資料來源:輸入 CSV 網址,下載並預覽(可選擇移除含空值的列,同原程式 dropna)
② 投放設定與排版:筆數、訊息方式、排版樣式、標題欄位、顯示欄位、標題 / 結尾附註
③ LINE 訊息預覽:以 LINE 綠色泡泡樣式即時預覽
④ 傳送到 LINE:輸入 Channel Access Token,勾選確認後廣播給所有好友

PWA 的 manifest / service worker / 圖示全部內建,不需要 static 資料夾。
"""

import base64
import html
import io
import os
import uuid
from datetime import datetime, timedelta, timezone

import gradio as gr
import pandas as pd
import requests
import urllib3
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

# ----------------------------------------------------------------------
# 常數
# ----------------------------------------------------------------------
DEFAULT_URL = "https://data.tainan.gov.tw/File/ResourceCsvDownload/24b8d897-7971-45f9-8489-b4918ee41ed7"
LINE_API = "https://api.line.me/v2/bot"
MAX_MESSAGES = 5        # LINE 單次 broadcast 最多 5 則訊息
MAX_CHARS = 5000        # 單則文字訊息上限
BODY_LIMIT = 4500       # 內文上限,保留空間給標題 / 結尾附註 / 頁碼
TIMEOUT = 20
SEP = "━━━━━━━━━━━━"
TW = timezone(timedelta(hours=8))

MODE_MERGE = "合併排版（推薦，省額度）"
MODE_SPLIT = "每筆獨立一則（最多 5 則）"
STYLE_BULLET = "條列（欄位名稱：內容）"
STYLE_SIMPLE = "精簡（只列內容）"
NO_TITLE = "（不使用，以序號為標題）"


# ----------------------------------------------------------------------
# ① 資料載入
# ----------------------------------------------------------------------
def fetch_csv(url: str):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; LineBroadcastBot/1.0)"}
    note = ""
    try:
        r = requests.get(url, headers=headers, timeout=TIMEOUT)
    except requests.exceptions.SSLError:
        # 部分政府網站憑證鏈不完整,僅讀取公開資料時退而略過驗證
        urllib3.disable_warnings()
        r = requests.get(url, headers=headers, timeout=TIMEOUT, verify=False)
        note = "(SSL 憑證驗證失敗,已略過驗證)"
    r.raise_for_status()
    return r.content, note


def parse_csv(content: bytes) -> pd.DataFrame:
    last_err = None
    for enc in ("utf-8-sig", "utf-8", "cp950", "big5"):
        try:
            df = pd.read_csv(io.BytesIO(content), encoding=enc, dtype=str)
            df.columns = [str(c).strip() for c in df.columns]
            return df
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise ValueError(f"CSV 解析失敗:{last_err}")


def load_data(url, do_dropna):
    """回傳:(DataFrame state, 狀態訊息, 預覽表, 欄位勾選, 標題欄位下拉)"""
    url = (url or "").strip()
    if not url:
        return None, "⚠️ 請輸入 CSV 網址", pd.DataFrame(), gr.update(), gr.update()
    try:
        content, note = fetch_csv(url)
        df = parse_csv(content)
    except requests.RequestException as e:
        return None, f"❌ 下載失敗:{e}", pd.DataFrame(), gr.update(), gr.update()
    except ValueError as e:
        return None, f"❌ {e}", pd.DataFrame(), gr.update(), gr.update()

    if df.empty:
        return None, "⚠️ CSV 內容為空", pd.DataFrame(), gr.update(), gr.update()

    msg_extra = ""
    if do_dropna:
        cleaned = df.dropna()
        if cleaned.empty:
            msg_extra = "(每一列都含空值,已保留全部資料)"
        else:
            removed = len(df) - len(cleaned)
            df = cleaned.reset_index(drop=True)
            if removed:
                msg_extra = f"(已移除 {removed} 筆含空值的列)"

    cols = list(df.columns)
    status = f"✅ 已載入 **{len(df)}** 筆、**{len(cols)}** 個欄位 {msg_extra}{note}"
    return (
        df,
        status,
        df.head(50),
        gr.update(choices=cols, value=cols[:10]),
        gr.update(choices=[NO_TITLE] + cols, value=NO_TITLE),
    )


# ----------------------------------------------------------------------
# ②③ 排版
# ----------------------------------------------------------------------
def fmt_val(v) -> str:
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v).strip()


def format_record(i, row, cols, style, title_col) -> str:
    title_text = fmt_val(row[title_col]) if title_col != NO_TITLE else ""
    lines = [f"【{i}】{title_text}" if title_text else f"【{i}】"]
    for c in cols:
        if c == title_col:
            continue
        v = fmt_val(row[c])
        if not v:
            continue
        lines.append(f"• {c}：{v}" if style == STYLE_BULLET else f"• {v}")
    return "\n".join(lines)


def pack_records(records, limit):
    msgs, cur = [], ""
    for r in records:
        if cur and len(cur) + 2 + len(r) > limit:
            msgs.append(cur)
            cur = r
        else:
            cur = f"{cur}\n\n{r}" if cur else r
    if cur:
        msgs.append(cur)
    return msgs


def build_messages(df, count, mode, style, title_col, cols, title, footer, show_time):
    if df is None or len(df) == 0:
        raise ValueError("請先在 ① 載入資料")
    if not cols:
        raise ValueError("請至少選擇一個要顯示的欄位")

    n = max(1, min(int(count or 1), 100, len(df)))
    sel = set(cols)
    ordered_cols = [c for c in df.columns if c in sel]
    rows = df.head(n)
    records = [
        format_record(i, row, ordered_cols, style, title_col)
        for i, (_, row) in enumerate(rows.iterrows(), start=1)
    ]

    if mode == MODE_SPLIT:
        if len(records) > MAX_MESSAGES:
            raise ValueError(
                f"「每筆獨立一則」最多 {MAX_MESSAGES} 則,請把筆數調到 {MAX_MESSAGES} 以內,或改用「合併排版」"
            )
        bodies = records
    else:
        bodies = pack_records(records, BODY_LIMIT)
        if len(bodies) > MAX_MESSAGES:
            raise ValueError(
                f"內容需要 {len(bodies)} 則,超過 LINE 單次最多 {MAX_MESSAGES} 則,請減少筆數或欄位"
            )

    title = (title or "").strip()[:100]
    footer = (footer or "").strip()[:300]
    head_lines = []
    if title:
        head_lines.append(title)
    if show_time:
        head_lines.append("🕐 " + datetime.now(TW).strftime("%Y-%m-%d %H:%M"))
    header = ("\n".join(head_lines) + "\n" + SEP) if head_lines else ""

    total = len(bodies)
    msgs = []
    for i, body in enumerate(bodies):
        m = body
        if i == 0 and header:
            m = f"{header}\n\n{m}"
        if i == total - 1 and footer:
            m = f"{m}\n\n{SEP}\n{footer}"
        if total > 1:
            m = f"{m}\n\n（{i + 1}/{total}）"
        msgs.append(m[:MAX_CHARS])
    return msgs, n


def render_chat(msgs) -> str:
    bubbles = []
    for m in msgs:
        safe = html.escape(m).replace("\n", "<br>")
        bubbles.append(f'<div class="bubble">{safe}</div>')
    return '<div class="line-chat">' + "".join(bubbles) + "</div>"


def do_preview(df, count, mode, style, title_col, cols, title, footer, show_time):
    try:
        msgs, n = build_messages(df, count, mode, style, title_col, cols, title, footer, show_time)
    except ValueError as e:
        return f"⚠️ {e}", ""
    return f"將投放 **{n}** 筆,共 **{len(msgs)}** 則 LINE 訊息", render_chat(msgs)


# ----------------------------------------------------------------------
# ④ 傳送
# ----------------------------------------------------------------------
def toggle_send(agree):
    return gr.update(interactive=bool(agree))


def do_send(token, df, count, mode, style, title_col, cols, title, footer, show_time, silent, agree):
    if not token or not token.strip():
        return "⚠️ 請先輸入 Channel Access Token"
    if not agree:
        return "⚠️ 請先勾選確認「廣播給所有好友」"
    try:
        msgs, n = build_messages(df, count, mode, style, title_col, cols, title, footer, show_time)
    except ValueError as e:
        return f"⚠️ {e}"

    payload = {
        "messages": [{"type": "text", "text": m} for m in msgs],
        "notificationDisabled": bool(silent),
    }
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Content-Type": "application/json",
        "X-Line-Retry-Key": str(uuid.uuid4()),  # 避免重送造成重複廣播
    }
    try:
        r = requests.post(f"{LINE_API}/message/broadcast", json=payload, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as e:
        return f"❌ 連線錯誤:{e}"

    if r.status_code == 200:
        return f"✅ 廣播成功!已將 **{n}** 筆資料以 **{len(msgs)}** 則訊息送給所有好友。"
    if r.status_code == 401:
        return "❌ Token 無效或已過期(401),請確認是 long-lived 的 Channel access token"
    if r.status_code == 429:
        return "❌ 已超過本月訊息額度或呼叫頻率過高(429)"
    return f"❌ 送出失敗({r.status_code}):{r.text}"


# ----------------------------------------------------------------------
# 介面(RWD + PWA)
# ----------------------------------------------------------------------
HEAD = """
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#06C755">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="LINE投放">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<link rel="icon" href="/icon-192.png">
<script>
if ("serviceWorker" in navigator) {
  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function (e) {
      console.warn("SW register failed", e);
    });
  });
}
var deferredPrompt = null;
function showInstallBtn() {
  if (document.getElementById("pwa-install") || !deferredPrompt) return;
  var b = document.createElement("button");
  b.id = "pwa-install";
  b.textContent = "📲 安裝 App";
  b.onclick = async function () {
    deferredPrompt.prompt();
    await deferredPrompt.userChoice;
    deferredPrompt = null;
    b.remove();
  };
  document.body.appendChild(b);
}
window.addEventListener("beforeinstallprompt", function (e) {
  e.preventDefault();
  deferredPrompt = e;
  if (document.body) showInstallBtn();
  else document.addEventListener("DOMContentLoaded", showInstallBtn);
});
window.addEventListener("appinstalled", function () {
  var b = document.getElementById("pwa-install");
  if (b) b.remove();
});
</script>
"""

CSS = """
.gradio-container { max-width: 980px !important; margin: 0 auto !important; }
.line-chat {
  background: #7f9ccb; border-radius: 12px; padding: 14px;
  max-height: 440px; overflow-y: auto;
  display: flex; flex-direction: column; align-items: flex-end; gap: 10px;
}
.line-chat .bubble {
  background: #a8e063; color: #111111;
  border-radius: 16px 4px 16px 16px;
  padding: 10px 14px; max-width: 72%;
  line-height: 1.6; word-break: break-word;
  box-shadow: 0 1px 2px rgba(0,0,0,.18);
}
.install-hint { font-size: 13px; color: #6b7280; padding: 4px 2px 18px; }
#pwa-install {
  position: fixed; right: 16px; bottom: 16px; z-index: 9999;
  background: #06C755; color: #fff; border: none; border-radius: 999px;
  padding: 12px 18px; font-size: 15px; cursor: pointer;
  box-shadow: 0 4px 12px rgba(0,0,0,.25);
}
@media (max-width: 768px) {
  .gradio-container { padding: 8px !important; }
  .line-chat .bubble { max-width: 90%; }
  button { min-height: 46px !important; font-size: 16px !important; }
  textarea, input { font-size: 16px !important; }  /* 避免 iOS 自動縮放 */
}
footer { display: none !important; }
"""


def build_demo():
    with gr.Blocks(
        title="臺南開放資料 ➜ LINE 投放",
        theme=gr.themes.Soft(primary_hue="green"),
        css=CSS,
        head=HEAD,
    ) as demo:
        gr.Markdown(
            "# 📣 臺南開放資料 ➜ LINE 投放\n"
            "下載開放資料、排版預覽,再廣播給官方帳號的所有好友。"
        )
        df_state = gr.State(None)

        # ① 資料來源 ---------------------------------------------------
        with gr.Accordion("① 資料來源", open=True):
            url = gr.Textbox(label="CSV 網址", value=DEFAULT_URL, lines=3)
            with gr.Row():
                dropna = gr.Checkbox(value=True, label="移除含空值的列(同原程式 dropna)", min_width=260)
                btn_load = gr.Button("🔄 下載 / 重新載入資料", min_width=260)
            load_status = gr.Markdown()
            df_preview = gr.Dataframe(
                label="資料預覽(前 50 筆,可左右滑動)", interactive=False, wrap=False
            )

        # ② 投放設定與排版 ---------------------------------------------
        with gr.Accordion("② 投放設定與排版", open=True):
            with gr.Row():
                count = gr.Number(
                    label="投放則數(筆數)", info="1~100 筆;LINE 單次廣播最多 5 則訊息",
                    value=10, minimum=1, maximum=100, precision=0, min_width=280,
                )
                mode = gr.Radio([MODE_MERGE, MODE_SPLIT], value=MODE_MERGE, label="訊息方式", min_width=280)
            with gr.Row():
                style = gr.Radio([STYLE_BULLET, STYLE_SIMPLE], value=STYLE_BULLET, label="排版樣式", min_width=280)
                title_col = gr.Dropdown(
                    choices=[NO_TITLE], value=NO_TITLE, label="標題欄位",
                    info="選一欄當每筆的標題", min_width=280,
                )
            cols = gr.CheckboxGroup(choices=[], value=[], label="要顯示的欄位")
            with gr.Row():
                title = gr.Textbox(label="訊息標題", value="📢 臺南市開放資料通知", min_width=280)
                footer = gr.Textbox(label="結尾附註", value="資料來源:臺南市政府資料開放平臺", min_width=280)
            show_time = gr.Checkbox(value=True, label="標題下方顯示時間")

        # ③ LINE 訊息預覽 ----------------------------------------------
        with gr.Accordion("③ LINE 訊息預覽", open=True):
            preview_info = gr.Markdown()
            preview_html = gr.HTML()

        # ④ 傳送到 LINE ------------------------------------------------
        with gr.Accordion("④ 傳送到 LINE", open=True):
            token = gr.Textbox(
                label="Channel Access Token", type="password",
                placeholder="貼上 LINE Developers 的 Channel access token (long-lived)",
                info="僅用於該次傳送,不會儲存或留下記錄。",
            )
            silent = gr.Checkbox(label="靜音推播(好友不會收到通知音)")
            agree = gr.Checkbox(label="我了解這會「廣播給所有好友」,且會消耗本月訊息額度")
            btn_send = gr.Button("🚀 傳送到 LINE", variant="primary", interactive=False)
            send_status = gr.Markdown()

        gr.HTML(
            '<div class="install-hint">📱 安裝成 App:Android / 電腦 Chrome 點右下角「安裝 App」;'
            "iPhone 請用 Safari 點「分享 → 加入主畫面」。</div>"
        )

        # 事件 ----------------------------------------------------------
        settings = [df_state, count, mode, style, title_col, cols, title, footer, show_time]
        load_outputs = [df_state, load_status, df_preview, cols, title_col]

        btn_load.click(load_data, [url, dropna], load_outputs).then(
            do_preview, settings, [preview_info, preview_html]
        )
        demo.load(load_data, [url, dropna], load_outputs).then(
            do_preview, settings, [preview_info, preview_html]
        )
        for comp in (count, mode, style, title_col, cols, title, footer, show_time):
            comp.change(do_preview, settings, [preview_info, preview_html], show_progress="hidden")

        agree.change(toggle_send, agree, btn_send)
        btn_send.click(
            do_send,
            [token, df_state, count, mode, style, title_col, cols, title, footer, show_time, silent, agree],
            send_status,
        )
    return demo


# ----------------------------------------------------------------------
# PWA 資源(全部內建,不需要 static 資料夾)
# ----------------------------------------------------------------------
ICON_192 = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAYAAABS3GwHAAAEB0lEQVR4nO3dUW7TQBSGURdVsCU2AotkI7AlnuAlESFqWsfxxPfOf85zG40897OnalW/fP757c8CoT4dvQA4kgCIJgCiCYBoAiCaAIgmAKIJgGgCIJoAiCYAogmAaAIgmgCIJgCiCYBoAiCaAIgmAKIJgGgCIJoAiCYAogmAaAIgmgCIJgCiCYBoAiCaAIgmAKIJgGgCIJoAiCYAogmAaAIgmgCIJgCiCYBor0cv4Bl+f/1x9BLa+vLr+9FLGOplxjfFG/hxZgtiqgAM/vPMEsIUARj843QPof0PwYb/WN2vf+sAul/8WXTeh5ZHoM4XfHbdjkTtngCGv7Zu+9MuANhTqwC63V1SddqnNgF0uqj02a8WAXS5mPyvw761CABGKR9Ah7sIt1Xfv/IBwEilA6h+92CdyvtYOgAYTQBEEwDRygZQ+dzI/aruZ9kA4BkEQDQBEE0ARBMA0QRANAEQTQBEEwDRBEA0ARBNAEQTANEEQDQBEE0ARBMA0QRANAEQTQBEEwDRBEC0iDfFb/HWu672/NceIz9/9NpnUvYleUdt2JqXvD2ytpGfP3rtj6r4Aj1HoAtrN2jrRo78/NFrn5UATu4djEpfP3otMxMA0QSw1DzSrP2+0WufnQCIJgCiCYBoAiCaAJbtvxxa+30jP3/02mcnAKIJ4OTeO2Klrx+9lpkJ4EKFI83Wzx+99ln5Y7gb/DXo/ir+8k0APE3FAByBiCYAogmAaAIgmgCIJgCiCYBoAiCaAIgmAKIJgGgCIJoAiCYAogmAaAIgmgCIJgCiCYBoAiCaAIgmAKKVDaDiv9Bgu6r7WTYAeAYBEE0ARCsdQNVzI/epvI+lA4DRygdQ+e7Bx6rvX/kAYKQWAVS/i/C2DvvWIoBl6XEx+afLfrUJYFn6XNR0nfapVQCwt3YBdLq7JOq2P2XfEbaG94jV0W3wz9o9AS51veiz6bwPrQNYlt4Xfwbdr3/rI9A1R6Ln6T74Z1MFcCaEcWYZ/LMpA7gmiO1mG/hrEQGsNTKU2Qepq/Y/BHdg+OsSwMmou7/hr00AAxn++l6PXkAFe9/9DX4fngA7M/y9CGBHhr+f+CPQHscfg9+XJ8CDDH9v0QE8evc3/P3FH4G2MPjziH4CbGH45xIbwJbjj+GfT2wA9zL8c4r8GeCeu7/Bn5snwDsM//wEcIPhzxB3BPro+GPws3gCXDD8eaICeO/ub/gzxR2Brhn8bFFPgGuGn5gAro8/hp9lCQrgkuHnLOJngPPd3+BzLeYJYPh5S0QAhp9bIgKAWwRANAEQTQBEEwDRBEA0ARBNAEQTANEEQDQBEE0ARBMA0QRANAEQTQBEEwDRBEA0ARBNAEQTANEEQDQBEE0ARBMA0f4C2O/7gyLZH+QAAAAASUVORK5CYII=")
ICON_512 = base64.b64decode("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")

MANIFEST = {
    "name": "臺南開放資料 ➜ LINE 投放",
    "short_name": "LINE投放",
    "description": "下載臺南開放資料、排版預覽,再廣播給 LINE 官方帳號的所有好友",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#ffffff",
    "theme_color": "#06C755",
    "lang": "zh-TW",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

SERVICE_WORKER = """
const CACHE = "line-broadcast-v3";
const ASSETS = ["/icon-192.png", "/icon-512.png"];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)));
  self.skipWaiting();
});
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});
self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (ASSETS.includes(url.pathname)) {
    e.respondWith(caches.match(req).then((r) => r || fetch(req)));
    return;
  }
  e.respondWith(fetch(req).catch(() => caches.match(req)));
});
"""

# ----------------------------------------------------------------------
# FastAPI:PWA 路由要先定義,最後才掛載 Gradio
# ----------------------------------------------------------------------
app = FastAPI()


@app.get("/manifest.json")
def manifest():
    return JSONResponse(MANIFEST, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return Response(
        SERVICE_WORKER,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/icon-192.png")
def icon_192():
    return Response(ICON_192, media_type="image/png")


@app.get("/icon-512.png")
def icon_512():
    return Response(ICON_512, media_type="image/png")


demo = build_demo()
demo.queue()
app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 7860)),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
