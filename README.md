# LINE 推播小幫手(Gradio + PWA + RWD)

輸入 Channel Access Token、排版預覽、確認後廣播給所有好友。

## 檔案
- `app.py`:主程式(PWA 的 manifest / service worker / 圖示都內建,不需要 static 資料夾)
- `requirements.txt`:套件清單
- `.python-version`、`render.yaml`:鎖定 Python 3.11 與 Render 設定(選用)

## 本機執行
    pip install -r requirements.txt
    python app.py        # http://localhost:7860

## 部署到 Render
1. 把 `app.py`、`requirements.txt`(建議連同 `.python-version`)放到 GitHub repo 根目錄。
2. Render → New → Web Service → 選此 repo。
3. Build Command:`pip install -r requirements.txt`  
   Start Command:`python app.py`
4. Environment 新增變數 `PYTHON_VERSION` = `3.11.9`(避免 Render 預設用太新的 Python 3.14)。
5. 部署完成後,用手機瀏覽器開網址 → 「加入主畫面」即可安裝成 PWA。

## 注意
- Token 由使用者在介面輸入,不寫入程式碼、不儲存。
- 單次 broadcast 最多 5 則;額度消耗 = 好友數 × 則數。
- Render 免費方案閒置會休眠,首次開啟需等待喚醒。
