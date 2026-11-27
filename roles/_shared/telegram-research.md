統一工具（查價、算錢、附連結、排方案都走 `travel.py`，直接原樣貼上輸出，不手抄改寫，免重跑檢查）：
- **國內出遊／週末／泡湯／住宿（一次產出 3 個能直接訂的方案）**：
  `"{{PYTHON}}" "{{ROOT}}/shared/travel-workflow/travel.py" local [--area <地區>] [--kind <類型>] [--date <YYYY-MM-DD>] [--nights <晚數>] [--guests <人數>] [--budget <金額>] [--cheaper-than <金額>]`
  - 地點庫不夠 3 家時，最多 `web_search` 3 次找官網（`web_extract` 讀不到網頁不用；`--official` 只用店家自己網域，訂房連結只用開過的頁面），跑 `travel.py add-place --name <名> --area <區> --kind <類> --official <官網> [--price-page <價格頁>] [--no-fee-listed]`（沒列收費不寫免費）收錄後再跑 `local`；還不夠就直接用已查到的出方案。
- **海外機票＋住宿（直接查價並產出含去回程航空與時間、行李說明、房型與可點擊連結的卡片，不走多輪看板覆核）**：
  `"{{PYTHON}}" "{{ROOT}}/shared/travel-workflow/travel.py" overseas <城市> <出發 YYYY-MM-DD> <回程 YYYY-MM-DD> [--guests <人數>] [--budget <金額>]`
  - 若回傳「未確認」或 Google 限流就照實回報，不重跑、不猜價。
- **使用者選定單一方案（附真照片、天氣、導航、預約話術）**：
  先跑 `travel.py photo "<名稱>" --page <官網頁> --for "<項目>"` 並用 `vision_analyze` 逐張確認拍到推薦項目本身（不用大廳或外觀代替），再跑：
  `"{{PYTHON}}" "{{ROOT}}/shared/travel-workflow/travel.py" detail --header "<日期 地點 人數>" --date <YYYY-MM-DD> [--budget <金額>] --item "<項目>=<單價>x<數>@<網址>" --place "<名稱>" --official <官網> [--phone <電話> --phone-source <網址>] [--photo <圖檔> --photo-caption "<說明>"] [--weather-place <地點>] [--route <起> <迄>] [--reservation-script]`
- **確認成行或收到訂房截圖**：跑 `travel.py remind --date <YYYY-MM-DD> --place "<名稱>" [--free-cancel <YYYY-MM-DD>] [--booking-ref <代號>]` 並請 `schedule-manager` 入曆。
- 事實底線：「有空房」只寫在開過填好日期的訂房頁時（官網起價寫「空房未確認」）；含早餐與評分須和價格同來源，不為湊預算改數字。只有非 `travel.py` 產生的手寫回覆，才需送出前跑 `"{{PYTHON}}" "{{ROOT}}/shared/travel-workflow/travel.py" check --text "<整則訊息>"`。
