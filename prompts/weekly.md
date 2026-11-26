整理一份繁體中文旅遊週摘要。遵循旅遊總指揮官的 SOUL.md。

先讀 `{{ROOT}}/shared/travel-workflow/` 的偏好和機會紀錄，用 `"{{PYTHON}}" "{{ROOT}}/shared/travel-workflow/calendar-check.py" --from <日期> --to <日期>` 查候選日期的行程（不要讀整份 calendar.json）。用本地終端機執行 `"{{PYTHON}}" "{{ROOT}}/profiles/travel-commander/scripts/travel-scout.py" --snapshot`，保留日期、掃描時間、價格和幣別。檢查 command-center 上等待查核的卡片並接續可完成的工作。

依出發地、目的地、旅行長度、整趟預算與完整時段篩選候選。必要時請 Trip Agent 補查，並向 Schedule Manager 核對時間。需要住宿和行程建議時載入 travel-planning 技能。提供少量適合考慮的候選，標記時段與費用的核對狀態、來源、查詢時間和下一個決定。沒有合適機會時如實說明。遇到資料缺口時，列出最多兩項影響最大的待確認條件。

地點庫：先跑 `"{{PYTHON}}" "{{ROOT}}/shared/travel-workflow/places.py" recheck` 重查過期的價格，再照 preferences.json 的 `interests` 每類各找 1–2 個北部（騎車或開車一兩小時內）地點庫還沒有的地方，最多 6 次搜尋；`places.py list` 看已有的。官網讀得到價格或寫著免費入園的，用 `places.py add --name --area --kind --official [--price-page]` 收進去。摘要裡只寫一行「地點庫新增 N 個、失效 M 個」。

近期熱門：另外上網找未來 6 週台灣正在熱門的活動或地點（花季、祭典、展覽、市集、節慶活動、新開幕景點），最多 6 次搜尋。每個都要打開主辦單位、官方或景點自己的頁面，確認今年的日期和地點；只有新聞或部落格寫到的標「未確認」，不推；來源日期不一致時以主辦單位頁為準。每個候選要有主辦單位或官方頁（給 `--official`），沒有就不推。用 calendar-check.py 留下使用者有空的日期，再依偏好的人數、出發地和預算篩選。最多推 3 個，用 `"{{PYTHON}}" "{{ROOT}}/shared/travel-workflow/format-plan.py" --header "近期熱門 <人數>人"` 加 `--package` 一次組好（每個一個 `--package`：`--place` 地點、`--dates` 日期、`--note` 為什麼熱門、`--transport` 交通與時間、`--item` 費用、`--official` 官方活動頁；只有一兩個符合就加 `--few-reason "只有…符合，因為…"`），原樣貼上它的輸出。已經在機會紀錄裡、或使用者回過不想去的，不再推。沒有符合的就寫「這週沒有適合的熱門活動」。最後用按鈕讓使用者選「想去」哪一個，選了就照一般規劃流程接著排。

訊息會在手機上閱讀：不用表格或程式碼區塊，改用每行一項的條列；第一行寫結論，整則控制在手機一到兩個畫面。需要使用者決定時，結尾寫一句「回覆這則訊息，我會用按鈕讓你選」，不要列一串文字選項。每個條列在手機上最多兩行。送出前用 `"{{PYTHON}}" "{{ROOT}}/shared/travel-workflow/phone-check.py" --text "<訊息，換行寫成 \n>"` 檢查一次，不是 OK 就改，最多重試兩次。一週內查過的資料直接沿用。
