執行 `"{{PYTHON}}" "{{ROOT}}/shared/travel-workflow/weekend-scout.py" --root "{{ROOT}}"` 檢查這個週末的空檔與天氣。
- 若 `status` 不是 `ready`（本週末已排滿），保持靜默不打擾使用者（回覆 `NO_REPLY`）。
- 若 `status` 是 `ready`：挑一個「沒下雨（降雨低於 50%）、開車或騎車 1 小時內、預算內」的完整一日遊方案（用 `page-prices.py` 查真價、`place-photo.py` 挑真照片、`format-plan.py` 一次組好含一鍵多點導航與預約話術的方案）。
- 用 `clarify` 一次送出方案全文，只給兩個按鈕：「就這個，幫我整理預約與導航」和「這週先不出門」。
