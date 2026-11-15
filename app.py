"""
LINE 推播小幫手 (Gradio + PWA + RWD)

功能:
1. 使用者自行輸入 Channel Access Token(不寫死在程式碼,也不儲存)
2. 可輸入訊息文字,或上傳 CSV(取代原本的 df1)
3. 可指定投放「則數」(LINE 單次 broadcast 最多 5 則),自動依段落平均切分
4. 排版優化:清爽段落 / 條列重點 / 公告風格,可加標題、結語、分頁標記
5. 先預覽(LINE 對話泡泡樣式),確認後才送出
6. PWA(manifest + service worker)、RWD(手機 / 平板 / 桌機)
"""

import base64
import html
import os
import re
import uuid

import gradio as gr
import pandas as pd
import requests
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

LINE_API = "https://api.line.me/v2/bot"
MAX_MESSAGES = 5          # LINE 單次 broadcast 最多 5 則訊息物件
MAX_CHARS = 5000          # 單則文字訊息上限
BODY_LIMIT = 4500         # 內文上限,保留空間給標題 / 結語 / 分頁標記
TIMEOUT = 15

STYLES = ["清爽段落", "條列重點", "公告風格"]


# ----------------------------------------------------------------------
# 文字處理
# ----------------------------------------------------------------------
def normalize(text: str) -> str:
    """清理空白、換行,讓 LINE 上顯示整齊。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    lines = []
    for line in text.split("\n"):
        line = line.rstrip()
        line = re.sub(r"(?<=\S)[ ]{2,}", " ", line)
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def read_csv_as_text(file) -> str:
    """把上傳的 CSV 轉成易讀文字(取代原本直接送 DataFrame 字串)。"""
    path = file if isinstance(file, str) else getattr(file, "name", None)
    if not path:
        return ""
    df = None
    for enc in ("utf-8-sig", "utf-8", "cp950", "big5"):
        try:
            df = pd.read_csv(path, encoding=enc)
            break
        except Exception:
            continue
    if df is None or df.empty:
        raise ValueError("CSV 讀取失敗或內容為空")

    df = df.head(100).fillna("")
    cols = list(df.columns)
    lines = []
    for i, (_, row) in enumerate(df.iterrows(), start=1):
        title = str(row[cols[0]]).strip()
        rest = [f"{c}:{str(row[c]).strip()}" for c in cols[1:] if str(row[c]).strip()]
        line = f"{i}. {title}"
        if rest:
            line += "\n   " + " ｜ ".join(rest)
        lines.append(line)
    return "\n".join(lines)


def apply_style(body: str, style: str) -> str:
    if style == "條列重點":
        out = []
        for line in body.split("\n"):
            s = line.strip()
            if not s:
                out.append("")
            elif re.match(r"^(\d+[\.\)、]|[•●·])\s*", s) or line.startswith("   "):
                out.append(line)
            elif re.match(r"^[-*]\s+", s):
                out.append("• " + re.sub(r"^[-*]\s+", "", s))
            else:
                out.append("• " + s)
        return "\n".join(out)
    return body


def split_blocks(text: str):
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) == 1:
        blocks = [ln for ln in text.split("\n") if ln.strip()]
    return blocks


def chunk_long(text: str, limit: int):
    """單一區塊超過字數上限時,依行切開。"""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # 極端情況:單行過長
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def split_into_n(text: str, n: int):
    """依段落把內容平均切成最多 n 則。"""
    blocks = split_blocks(text)
    n = max(1, min(n, len(blocks)))
    total = sum(len(b) for b in blocks)
    target = total / n

    groups, cur, cur_len = [], [], 0
    for i, b in enumerate(blocks):
        remaining_blocks = len(blocks) - i
        remaining_groups = n - len(groups)
        should_close = cur and len(groups) < n - 1 and (
            remaining_blocks <= remaining_groups - 1 or cur_len + len(b) > target
        )
        if should_close:
            groups.append("\n\n".join(cur))
            cur, cur_len = [], 0
        cur.append(b)
        cur_len += len(b)
    if cur:
        groups.append("\n\n".join(cur))

    final = []
    for g in groups:
        final.extend(chunk_long(g, BODY_LIMIT))
    return final


def build_messages(text, csv_file, style, title, footer, n, paginate):
    parts = []
    if text and text.strip():
        parts.append(normalize(text))
    if csv_file:
        parts.append(read_csv_as_text(csv_file))
    body = "\n\n".join(p for p in parts if p)
    if not body:
        raise ValueError("請輸入訊息內容,或上傳 CSV 檔")

    body = apply_style(body, style)
    n = int(n or 1)
    msgs = split_into_n(body, n)
    if len(msgs) > MAX_MESSAGES:
        raise ValueError(
            f"內容太長,需要 {len(msgs)} 則,超過 LINE 單次最多 {MAX_MESSAGES} 則的限制,請縮減內容"
        )

    title = (title or "").strip()[:100]
    footer = (footer or "").strip()[:300]
    sep = "━━━━━━━━━━"
    total = len(msgs)

    result = []
    for i, m in enumerate(msgs):
        if i == 0 and title:
            m = f"📢 {title}\n{sep}\n{m}" if style == "公告風格" else f"【{title}】\n\n{m}"
        if i == total - 1 and footer:
            m = f"{m}\n{sep}\n{footer}" if style == "公告風格" else f"{m}\n\n{footer}"
        if paginate and total > 1:
            m = f"{m}\n\n({i + 1}/{total})"
        result.append(m[:MAX_CHARS])
    return result


def render_preview(messages) -> str:
    items = []
    for i, m in enumerate(messages, start=1):
        safe = html.escape(m).replace("\n", "<br>")
        items.append(
            f'<div class="bubble-meta">第 {i} 則 · {len(m)} 字</div>'
            f'<div class="bubble">{safe}</div>'
        )
    return '<div class="line-preview">' + "".join(items) + "</div>"


# ----------------------------------------------------------------------
# LINE API
# ----------------------------------------------------------------------
def _headers(token: str):
    return {"Authorization": f"Bearer {token.strip()}"}


def test_token(token: str) -> str:
    if not token or not token.strip():
        return "⚠️ 請先輸入 Channel Access Token"
    try:
        r = requests.get(f"{LINE_API}/info", headers=_headers(token), timeout=TIMEOUT)
        if r.status_code == 401:
            return "❌ Token 無效或已過期(401)"
        if r.status_code != 200:
            return f"❌ 驗證失敗({r.status_code}):{r.text}"
        info = r.json()
        name = info.get("displayName", "(未知)")
        out = f"✅ Token 有效,官方帳號:**{name}**"

        q = requests.get(f"{LINE_API}/message/quota", headers=_headers(token), timeout=TIMEOUT)
        c = requests.get(
            f"{LINE_API}/message/quota/consumption", headers=_headers(token), timeout=TIMEOUT
        )
        if q.status_code == 200 and c.status_code == 200:
            qj, cj = q.json(), c.json()
            if qj.get("type") == "limited":
                out += f"\n\n本月訊息額度:已用 {cj.get('totalUsage', '?')} / {qj.get('value', '?')} 則"
            else:
                out += f"\n\n本月已用 {cj.get('totalUsage', '?')} 則(無額度上限)"
        return out
    except requests.RequestException as e:
        return f"❌ 連線錯誤:{e}"


def do_preview(text, csv_file, style, title, footer, n, paginate):
    try:
        msgs = build_messages(text, csv_file, style, title, footer, n, paginate)
    except ValueError as e:
        return "", f"⚠️ {e}"
    note = f"✅ 已排版為 **{len(msgs)} 則** 訊息。"
    if len(msgs) < int(n or 1):
        note += f"(內容段落不足,實際只切成 {len(msgs)} 則)"
    note += "\n\n⚠️ 廣播會計入「收件人數 × 則數」的額度,請確認後再送出。"
    return render_preview(msgs), note


def do_send(token, text, csv_file, style, title, footer, n, paginate, confirm):
    if not token or not token.strip():
        return "⚠️ 請先輸入 Channel Access Token"
    if not confirm:
        return "⚠️ 請先勾選「我確認要廣播給所有好友」"
    try:
        msgs = build_messages(text, csv_file, style, title, footer, n, paginate)
    except ValueError as e:
        return f"⚠️ {e}"

    payload = {"messages": [{"type": "text", "text": m} for m in msgs]}
    headers = {
        **_headers(token),
        "Content-Type": "application/json",
        "X-Line-Retry-Key": str(uuid.uuid4()),  # 避免重送造成重複廣播
    }
    try:
        r = requests.post(f"{LINE_API}/message/broadcast", json=payload, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as e:
        return f"❌ 連線錯誤:{e}"

    if r.status_code == 200:
        return f"✅ 廣播成功!共送出 **{len(msgs)} 則** 訊息給所有好友。"
    if r.status_code == 401:
        return "❌ Token 無效或已過期(401)"
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
<meta name="apple-mobile-web-app-title" content="LINE推播">
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
</script>
"""

CSS = """
.gradio-container { max-width: 1000px !important; margin: 0 auto !important; }
.line-preview { background: #849ebf; border-radius: 16px; padding: 14px; }
.line-preview .bubble-meta { font-size: 12px; color: #eef3f9; margin: 8px 0 4px 4px; }
.line-preview .bubble {
  background: #ffffff; color: #111111; border-radius: 4px 18px 18px 18px;
  padding: 10px 14px; max-width: 92%; line-height: 1.6;
  word-break: break-word; box-shadow: 0 1px 2px rgba(0,0,0,.15);
}
@media (max-width: 768px) {
  .gradio-container { padding: 8px !important; }
  .line-preview .bubble { max-width: 100%; }
  button { min-height: 46px !important; font-size: 16px !important; }
  textarea, input { font-size: 16px !important; }  /* 避免 iOS 自動縮放 */
}
footer { display: none !important; }
"""


def build_demo():
    with gr.Blocks(
        title="LINE 推播小幫手",
        theme=gr.themes.Soft(primary_hue="green"),
        css=CSS,
        head=HEAD,
    ) as demo:
        gr.Markdown("# 📣 LINE 推播小幫手\n輸入 Token、排版預覽、確認後一鍵廣播。Token 只用於本次請求,不會被儲存。")

        with gr.Row():
            with gr.Column(scale=1, min_width=320):
                token = gr.Textbox(
                    label="Channel Access Token(API Key)",
                    type="password",
                    placeholder="貼上 LINE Messaging API 的 Channel access token",
                )
                btn_test = gr.Button("🔑 驗證 Token / 查看額度")
                token_status = gr.Markdown()

                text = gr.Textbox(
                    label="訊息內容",
                    lines=10,
                    placeholder="輸入要廣播的文字,段落之間空一行,切分時會以段落為單位。",
                )
                csv_file = gr.File(label="(選填)上傳 CSV,轉成條列內容", file_types=[".csv"])

            with gr.Column(scale=1, min_width=320):
                n = gr.Slider(1, MAX_MESSAGES, value=1, step=1, label="投放則數(LINE 單次最多 5 則)")
                style = gr.Radio(STYLES, value="清爽段落", label="排版風格")
                title = gr.Textbox(label="標題(選填)", max_lines=1)
                footer = gr.Textbox(label="結語 / 備註(選填)", max_lines=2)
                paginate = gr.Checkbox(value=True, label="多則時加上頁碼 (1/3)")

                btn_preview = gr.Button("👀 排版預覽", variant="secondary")
                confirm = gr.Checkbox(label="我確認要廣播給所有好友")
                btn_send = gr.Button("🚀 送出廣播", variant="primary")

        status = gr.Markdown()
        preview = gr.HTML(label="預覽")

        btn_test.click(test_token, inputs=token, outputs=token_status)
        btn_preview.click(
            do_preview,
            inputs=[text, csv_file, style, title, footer, n, paginate],
            outputs=[preview, status],
        )
        btn_send.click(
            do_send,
            inputs=[token, text, csv_file, style, title, footer, n, paginate, confirm],
            outputs=status,
        )
    return demo


# ----------------------------------------------------------------------
# PWA 資源(全部內建在程式碼裡,不需要 static 資料夾)
# ----------------------------------------------------------------------
ICON_192 = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAYAAABS3GwHAAAEB0lEQVR4nO3dUW7TQBSGURdVsCU2AotkI7AlnuAlESFqWsfxxPfOf85zG40897OnalW/fP757c8CoT4dvQA4kgCIJgCiCYBoAiCaAIgmAKIJgGgCIJoAiCYAogmAaAIgmgCIJgCiCYBoAiCaAIgmAKIJgGgCIJoAiCYAogmAaAIgmgCIJgCiCYBoAiCaAIgmAKIJgGgCIJoAiCYAogmAaAIgmgCIJgCiCYBor0cv4Bl+f/1x9BLa+vLr+9FLGOplxjfFG/hxZgtiqgAM/vPMEsIUARj843QPof0PwYb/WN2vf+sAul/8WXTeh5ZHoM4XfHbdjkTtngCGv7Zu+9MuANhTqwC63V1SddqnNgF0uqj02a8WAXS5mPyvw761CABGKR9Ah7sIt1Xfv/IBwEilA6h+92CdyvtYOgAYTQBEEwDRygZQ+dzI/aruZ9kA4BkEQDQBEE0ARBMA0QRANAEQTQBEEwDRBEA0ARBNAEQTANEEQDQBEE0ARBMA0QRANAEQTQBEEwDRBEC0iDfFb/HWu672/NceIz9/9NpnUvYleUdt2JqXvD2ytpGfP3rtj6r4Aj1HoAtrN2jrRo78/NFrn5UATu4djEpfP3otMxMA0QSw1DzSrP2+0WufnQCIJgCiCYBoAiCaAJbtvxxa+30jP3/02mcnAKIJ4OTeO2Klrx+9lpkJ4EKFI83Wzx+99ln5Y7gb/DXo/ir+8k0APE3FAByBiCYAogmAaAIgmgCIJgCiCYBoAiCaAIgmAKIJgGgCIJoAiCYAogmAaAIgmgCIJgCiCYBoAiCaAIgmAKKVDaDiv9Bgu6r7WTYAeAYBEE0ARCsdQNVzI/epvI+lA4DRygdQ+e7Bx6rvX/kAYKQWAVS/i/C2DvvWIoBl6XEx+afLfrUJYFn6XNR0nfapVQCwt3YBdLq7JOq2P2XfEbaG94jV0W3wz9o9AS51veiz6bwPrQNYlt4Xfwbdr3/rI9A1R6Ln6T74Z1MFcCaEcWYZ/LMpA7gmiO1mG/hrEQGsNTKU2Qepq/Y/BHdg+OsSwMmou7/hr00AAxn++l6PXkAFe9/9DX4fngA7M/y9CGBHhr+f+CPQHscfg9+XJ8CDDH9v0QE8evc3/P3FH4G2MPjziH4CbGH45xIbwJbjj+GfT2wA9zL8c4r8GeCeu7/Bn5snwDsM//wEcIPhzxB3BPro+GPws3gCXDD8eaICeO/ub/gzxR2Brhn8bFFPgGuGn5gAro8/hp9lCQrgkuHnLOJngPPd3+BzLeYJYPh5S0QAhp9bIgKAWwRANAEQTQBEEwDRBEA0ARBNAEQTANEEQDQBEE0ARBMA0QRANAEQTQBEEwDRBEA0ARBNAEQTANEEQDQBEE0ARBMA0f4C2O/7gyLZH+QAAAAASUVORK5CYII=")
ICON_512 = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAYAAAD0eNT6AAANB0lEQVR4nO3cTY4UxxpAUbAQbImNwCLZCGyJER484WcD3V1dlZnxc88ZW+4MlSK+W1Ep3r7/+unHGwAg5a/RDwAAXE8AAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAh6N/oBaPn+8cvoR4Cpffj2efQjEPH2/ddPP0Y/BPsx6OFYwoCjCQAOYeDDtQQBjxIA3M3QhzmIAe4hAHg1gx/mJAR4DQHATQx9WIsY4CUCgGcZ/LA2IcBTBAB/ZPDDXoQAvxIA/IfBD3sTAvzkXwLkH4Y/7M8+5yc3ADgQIMptQJsbgDjDH7rs/zYBEGbzA86BLj8BBNnwwJ/4SaDFDUCM4Q88xfnQIgBCbG7gJc6JDgEQYVMDt3JeNAiAAJsZeC3nxv4EwOZsYuBezo+9CYCN2bzAo5wj+xIAm7JpgaM4T/YkADZkswJHc67sRwBsxiYFzuJ82YsA2IjNCZzNObMPAQAAQQJgE6ocuIrzZg8CYAM2I3A15876BMDibEJgFOfP2gQAAAQJgIWpb2A059C6BMCibDpgFs6jNQkAAAgSAAtS28BsnEvrEQAAECQAFqOygVk5n9YiAAAgSAAsRF0Ds3NOrUMAAECQAACAIAGwCNdqwCqcV2sQAAAQJAAWoKaB1Ti35icAACBIAABAkACYnGs0YFXOr7kJAAAIEgAAECQAACBIAEzM72fA6pxj8xIAABAkAAAgSAAAQJAAAIAgAQAAQQJgUt6cBXbhPJuTAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAh6N/oBYIQP3z6/+N98//jlgie5VnHdxTXDLd6+//rpx+iH4HcOpOPdMgiesvLnUVx3cc2ze+Qz4RwCYFIOoeMcefCs9LkU111c8yoEwHwEwKQcPo8788CZ+fMprru45tUIgPl4CZAtnX3YzHqYFdddXDMcQQCwnasO7NkGQ3HdxTXDUQQAW7n6oJ5lMBTXXVwzHEkAsI1RB/TowVBcd3HNcDQBwBZGH8zVgTTi7xfXDGcQACxvlgO5eiV95XMU1wxnEQAAECQAWNps38Sqb6Vf8TzFNcOZBAAABAkAljXrN7DqP0xz5nMV1wxnEwAAECQAACBIALCk2a9ez3q+4rqLa4YrCAAACBIAABAkAAAgSAAAQJAAAIAgAQAAQQIAAIIEAAAECQCW9P3jl9GP8Kyznq+47uKa4QoCAACCBAAABAkAljXr1evZz1Vcd3HNcDYBAABBAoClzfYN7KrnKa67uGY4kwAAgCABwPJm+SZ29XMU111cM5xFALCF0QfyqL9fXHdxzXAGAcA2DOHG3x35t0d/1nAkAcBWXMPv+fdmeIYZ1gxHEgBsx5v4e/ydWxTXDEcRAGzJP8az5v//HsU1wxHevv/66cfoh+B3Dp3jfPj2+bD/10qfS3HdxTWv4sjPhmMIgEk5fI73yAG08udRXHdxzbMTAPMRAJNyCJ3rlsNox8+guO7immckAOYjACblQAJ2IgDm4yVAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBAAABAkAAAgSAAAQJAAAIEgAAECQAACAIAEAAEECAACCBMCkPnz7PPoRAA7hPJuTAACAIAEAAEECAACCBAAABAkAAAgSABPz5iywOufYvAQAAAQJAAAIEgAAECQAJuf3M2BVzq+5CQAACBIAABAkABbgGg1YjXNrfgIAAIIEwCLUNLAK59UaBAAABAkAAAgSAAtxrQbMzjm1DgEAAEECYDHqGpiV82ktAgAAggTAglQ2MBvn0noEAAAECYBFqW1gFs6jNQmAhdl0wGjOoXUJAAAIEgCLU9/AKM6ftQmADdiEwNWcO+sTAJuwGYGrOG/2IAAAIEgAbESVA2dzzuxDAGzG5gTO4nzZiwDYkE0KHM25sh8BsCmbFTiK82RPAmBjNi3wKOfIvgTA5mxe4F7Oj70JgACbGHgt58b+BECEzQzcynnRIABCbGrgJc6JDgEQY3MDT3E+tLx9//XTj9EPwRjfP34Z/QjABAz+JjcAYTY94BzoEgBxNj902f9tfgLgH34SgAaDnzdv3ADwLw4F2J99zk9uAPgjtwGwF4OfXwkAniUEYG0GP08RANxECMBaDH5eIgB4NTEAczL0eQ0BwN2EAMzB4OceAoBDiAG4lqHPowQApxAEcCwDn6MJAC4lDOB5Bj1XEQBsoxwXhgbwWu9GPwBwP4MfuJd/ChgWZfgDjxAAbKF2/W/4A48SALAYwx84gncAWF7l27/BDxzJDQAswPAHjiYAYHKGP3AGAcDSdr/+N/yBs3gHACZk8ANncwMAkzH8gSsIAJa14/W/4Q9cxU8AMAGDH7iaGwCWtNO3f8MfGEEAwECGPzCKAIBBDH9gJO8AsJzVr/8NfmAGbgDgQoY/MAsBABcx/IGZCACWsur1v+EPzMY7AHAigx+YlRsAlrHat3/DH5iZAIATGP7A7PwEAAcy+IFVuAFgCStc/xv+wEoEABzA8AdWIwCY3uzf/g1/YEXeAYA7GfzAytwAwB0Mf2B1AoCpzXj9b/gDOxAA8AqGP7AL7wDADQx+YDduAJjWLNf/hj+wIwEAzzD8gV0JAKY0w7d/wx/YmXcA4BcGP1DgBgD+xfAHKgQA0xl1/W/4AyV+AiDP4AeK3AAwlau//Rv+QJUAIMvwB8oEAEmGP1DnHQCmccX1v8EP8D9uAMgw/AH+TwCQYPgD/JcAYApnXv8b/gC/8w4A2zL4AZ7mBoDhzvj2b/gDPE8AsB3DH+BlfgJgGwY/wO3cADDUUdf/hj/A6wgAlmf4A7yeAGBphj/AfbwDwDCPXP8b/ACPcQPAcgx/gMcJAIa499u/4Q9wDAHAMgx/gON4B4DpGfwAx3MDwOVec/1v+AOcQwAwLcMf4Dx+AuBSt3z7N/gBzucGgKkY/gDXEABMw/AHuI4A4DLPXf8b/gDX8g4AQxn8AGO4AWAYwx9gHAHAJX69/jf8AcYSAFzO8AcYzzsAnO7nt3+DH2AebgC4hOEPMBcBwOkMf4D5CAAACBIAABAkAAAgSAAAQJAAAIAgAQAAQQIAAIIEAAAECQAACBIAABAkAAAgSAAAQJAAAIAgAQAAQQIAAIIEAAAECQAACBIAABAkAAAgSAAAQJAAAIAgAQAAQQIAAIIEAAAECQAACBIAABAkAAAgSAAAQJAAAIAgAQAAQQIAAIIEAAAECQAACBIAABAkAAAgSAAAQJAAAIAgAQAAQQIAAIIEAAAECQAACBIAABAkAAAgSAAAQJAAAIAgAQAAQQIAAIIEAAAECQAACBIAABAkAAAgSAAAQJAAAIAgAQAAQQIAAIIEAAAECQAACBIAABAkAAAgSAAAQJAAAIAgAQAAQQIAAIIEAAAECQAACBIAABAkAAAgSAAAQJAAAIAgAQAAQX8DfiqWRRTvnEcAAAAASUVORK5CYII=")

MANIFEST = {
    "name": "LINE 推播小幫手",
    "short_name": "LINE推播",
    "description": "輸入 Channel Access Token,排版優化後一鍵廣播 LINE 訊息",
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
const CACHE = "line-broadcast-v2";
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
# FastAPI:提供 PWA 檔案 + 掛載 Gradio(PWA 路由要先定義,最後才掛載 Gradio)
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
        proxy_headers=True,          # Render 在前面有 proxy,讓 https 判斷正確
        forwarded_allow_ips="*",
    )
