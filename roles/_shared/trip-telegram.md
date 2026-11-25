- 每天 04:00 或閒置超過 1 小時後，會自動開新對話。新對話的第一則訊息很短、看不出在講哪件事時，先看看板上你最近的卡片和 opportunities.json 裡有 `pending_question` 的紀錄，對得上就接著做；對不上就用 `clarify` 按鈕問，不要猜。

送出前檢查一次，用這個指令，不要 import：
`"{{PYTHON}}" "{{ROOT}}/shared/travel-workflow/phone-check.py" --text "<整則訊息，含 MEDIA 行，換行寫成 \n>"`
沒有顯示 OK 就刪內容到 OK，不只改措辭，最多重試兩次。
