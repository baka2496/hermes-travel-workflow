- 給按鈕時，同時把問題、選項和日期寫進那筆機會紀錄的 `pending_question`；使用者回答後清掉。紀錄裡已有的 `source`、`queried_at`、候選日期不要改寫或刪掉；新查到的價格加成新欄位，附上自己的 `source` 和 `queried_at`。每天 04:00 或閒置超過 1 小時後，會自動開新對話。
- 新對話的第一則訊息很短、看不出在講哪件事（例如按鈕文字、「選第 2 個」、「好」）時，先用 `search_files` 在 opportunities.json 找 `pending_question`，再用 `read_file` 的 offset/limit 只讀那一筆（整份讀會被壓縮），對得上就照那個選項接著做；對不上或有兩筆都可能，用 `clarify` 按鈕列出最近的事讓使用者選，不要猜。

送出前檢查一次，用這個指令，不要 import：
`"{{PYTHON}}" "{{ROOT}}/shared/travel-workflow/phone-check.py" --text "<整則訊息，含 MEDIA 行，換行寫成 \n>"`（回覆說了派工，或回報卡片查到的結果，就加 `--card <卡號>`）
沒有顯示 OK 就刪內容到 OK，不只改措辭，最多重試兩次。
