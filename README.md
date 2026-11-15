# 臺南開放資料 ➜ LINE 投放(Gradio + PWA + RWD)

① 載入 CSV 開放資料 → ② 設定筆數與排版 → ③ LINE 泡泡預覽 → ④ 輸入 Token 廣播給所有好友。

## 檔案
- `app.py`:主程式(PWA 的 manifest / service worker / 圖示都內建,不需要 static 資料夾)
- `requirements.txt`:套件清單
- `.python-version`、`render.yaml`:鎖定 Python 3.11(選用)

## 本機執行
    pip install -r requirements.txt
    python app.py        # http://localhost:7860

## 部署到 Render
1. 把 `app.py`、`requirements.txt`(建議連同 `.python-version`)放到 GitHub repo 根目錄。
2. Render → New → Web Service → 選此 repo。
3. Build Command:`pip install -r requirements.txt`  
   Start Command:`python app.py`
4. Environment 新增 `PYTHON_VERSION` = `3.11.9`。
5. 部署完成後,Android / 電腦 Chrome 點右下角「安裝 App」;iPhone 用 Safari「分享 → 加入主畫面」。

## 注意
- Token 由使用者在介面輸入,不寫入程式碼、不儲存。
- 單次 broadcast 最多 5 則;額度消耗 = 好友數 × 則數。
- 「每筆獨立一則」最多 5 筆;超過請用「合併排版」(內容太長會自動拆成多則)。
