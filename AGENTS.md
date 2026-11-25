# 給在這個專案工作的 AI（Claude、Antigravity）

## 互相審查
Claude 和 Antigravity 會互相檢查對方的修改。`git push` 時，`.githooks/pre-push` 會自動把這次要推的提交交給**另一個** AI 審查，審查意見會直接印在 push 的輸出裡（程式在 `tools/cross-review.py`）：
- Claude（Claude Code）推的，由 Antigravity 審。
- Antigravity 或主人推的，由 Claude 審。

新的 clone 要先跑一次 `py tools/cross-review.py install` 開啟。審查不會擋住推送；審查者找不到、太慢或出錯時，只會印一行警告。

推送後要讀輸出裡的「審查意見」：
1. 第一行是「結論：沒問題」就繼續做。
2. 有問題時，逐點自己查證（打開檔案、跑測試、對照原始資料），不要照單全收：
   - 真的問題就修好、補測試，再推一次。只有新修的部分會再審一次。
   - 判斷不是問題的，回報主人時寫清楚「審查者說 X，我不同意，因為 Y」。
3. **最多修一輪**。修完再推，如果第二次審查又提出新意見，不要再改，直接整理給主人決定，免得兩個 AI 一直來回。
4. 回報給主人時要附上：審查者是誰、提了幾點、哪些修了、哪些沒修和原因。

想手動請對方審：`py tools/cross-review.py review --reviewer agy`（Antigravity 審）或 `--reviewer claude`（Claude 審）。暫時關掉：設定環境變數 `CROSS_REVIEW=off`。
