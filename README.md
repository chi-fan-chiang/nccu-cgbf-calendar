# 🎓 政大國際金融學院 (CGBF) 課表與行事曆產生器

專為**國立政治大學國際金融學院（NCCU College of Global Banking and Finance）**打造的課表時間查詢與 `.ics` 行事曆匯出工具。

🌐 **線上立即使用（免安裝）：[https://nccu-cgbf-calendar.netlify.app](https://nccu-cgbf-calendar.netlify.app)**

---

## ✨ 功能特色

1. **完整課程清單與即時勾選**：內建學期全部課程與精確上課時段（自動排除教室代碼），勾選修習課程後立即產生依日期先後排序的個人課表。
2. **每次開啟自動連線選課系統確認異動**：透過雲端 Serverless Function（`netlify/functions/live-schedule.mjs`）即時讀取政大國金選課系統首頁最新「課程公告」與「課程異動公告」，自動套用臨時停課、取消或調補課日期。
3. **一鍵匯出 `.ics` 與手機 QR Code 掃描匯入**：
   - 支援電腦直接下載 `.ics` 行事曆檔。
   - 內建動態 QR Code（由 `netlify/functions/ics.mjs` 即時產生標準 `text/calendar` 回應），使用 iPhone / Android 相機一掃即可一鍵匯入手機行事曆。
4. **跨學期通用免維護**：換新學期時，可直接將選課系統「課程資料 (`/courses`)」與「我的課程 (`/enrollments`)」畫面複製貼上至網頁，自動解析新學期課表。
5. **本機背景自動化擷取工具（選用）**：提供 `cgbf_calendar.py` 與 `啟動選課擷取工具.bat`，可於本機輸入學號密碼後在背景自動擷取個人選課清單並匯出 `.ics`。

---

## 📂 專案結構

- `index.html`：前端單頁式應用程式（包含課程勾選、時間表預覽、QR Code 產生與跨學期貼上解析器）
- `netlify/functions/live-schedule.mjs`：每次開啟網頁時自動連線選課系統確認最新公告與停課異動的雲端函式
- `netlify/functions/ics.mjs`：提供手機掃描 QR Code 時直接下載對應課程 `.ics` 行事曆的雲端函式
- `cgbf_calendar.py` / `啟動選課擷取工具.bat`：Windows 本機版 Python + Playwright 自動化擷取腳本
- `netlify.toml`：Netlify 部署與 Serverless Functions 設定檔
