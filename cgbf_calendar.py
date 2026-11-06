#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
國立政治大學國際金融學院 (CGBF) 選課系統課程時間擷取與行事曆 (.ics) 匯出工具
功能：
1. 自動抓取選課系統「課程資料 (/courses)」中每門課最精確的上課日期、確切時段（如 18:30～21:30、08:00～17:00）與教室備註，
   並結合登入首頁「課程公告」的停課／異動註記。
2. 抓出使用者在「我的課程 (/enrollments)」選修的課程清單（科目代碼、科目名稱、授課教師、修課狀態）。
3. 自動對照輸出「您有課的時間總表」文字檔與標準 `.ics` 行事曆檔（支援 Google Calendar / Apple 行事曆 / Outlook）。
"""

import argparse
import datetime
import getpass
import json
import os
import re
import smtplib
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

BASE_URL = "https://selectcourse.cgbf.nccu.edu.tw"
LOGIN_URL = f"{BASE_URL}/login"
COURSES_URL = f"{BASE_URL}/courses"
ENROLLMENTS_URL = f"{BASE_URL}/enrollments"
LOGIN_HOST = "selectcourse.cgbf.nccu.edu.tw"

WEEKDAY_NAMES = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


def resolve_host_via_public_dns(hostname: str, fallback_ip: str = "54.95.148.43") -> str:
    """透過 8.8.8.8 查詢最新 IP，避免部分本地網路預設 DNS 無法解析 selectcourse.cgbf.nccu.edu.tw"""
    try:
        out = subprocess.check_output(
            ["nslookup", hostname, "8.8.8.8"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        ips = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", out)
        valid_ips = [ip for ip in ips if ip != "8.8.8.8"]
        if valid_ips:
            return valid_ips[-1]
    except Exception:
        pass
    return fallback_ip


@dataclass
class CourseSession:
    """單一堂課的確切日期與時間區間"""
    date: datetime.date
    start_time: str  # "18:30"
    end_time: str    # "21:30"
    note: str = ""
    is_cancelled: bool = False


@dataclass
class CourseCatalogItem:
    """從 /courses (課程資料) 解析出的完整課程資訊"""
    course_code: str
    course_name: str
    teacher: str = ""
    credits: str = ""
    category: str = ""
    classroom: str = ""
    remark: str = ""
    sessions: List[CourseSession] = field(default_factory=list)


@dataclass
class EnrolledCourseItem:
    """從 /enrollments (我的課程) 解析出的使用者已選課程"""
    course_code: str
    course_name: str
    teacher: str = ""
    credits: str = ""
    category: str = ""
    status: str = ""


def normalize_course_name(name: str) -> str:
    """正規化課程名稱以便比對（去除『英文授課』標籤、空白與括號）"""
    name = re.sub(r"英文授課", "", name)
    name = re.sub(r"\s+", "", name)
    name = re.sub(r"[（(].*?[)）]", "", name)
    return name.strip()


def parse_announcement_cancellations(html_content: str) -> Dict[str, Dict[Tuple[int, int], Tuple[str, bool]]]:
    """
    從首頁『課程公告』解析各門課特定日期的備註或停課資訊。
    回傳：{正規化課程名稱: {(month, day): (note, is_cancelled)}}
    """
    soup = BeautifulSoup(html_content, "html.parser")
    notes_map: Dict[str, Dict[Tuple[int, int], Tuple[str, bool]]] = {}

    def process_line(c_name: str, date_str: str):
        norm = normalize_course_name(c_name)
        if norm not in notes_map:
            notes_map[norm] = {}
        cleaned_str = re.sub(r"[（(]原[^）)]*?[）)]", "", date_str)
        for token in re.split(r"[、,，]", cleaned_str):
            token = token.strip()
            m = re.search(r"(\d{1,2})\s*/\s*(\d{1,2})(.*)", token)
            if not m:
                continue
            month, day = int(m.group(1)), int(m.group(2))
            suffix = re.sub(r"^[（(]|[)）]$", "", m.group(3).strip()).strip()
            if suffix:
                is_cancel = any(k in suffix for k in ("停課", "取消", "暫停"))
                notes_map[norm][(month, day)] = (suffix, is_cancel)

    for p in soup.find_all("p"):
        text = p.get_text(" ", strip=True)
        if "：" in text or ":" in text:
            parts = re.split(r"[：:]", text, maxsplit=1)
            if len(parts) == 2 and re.search(r"\d{1,2}/\d{1,2}", parts[1]):
                process_line(parts[0], parts[1])

    for li in soup.find_all("li"):
        strong = li.find("strong")
        if strong:
            c_name = strong.get_text(strip=True)
            full_text = li.get_text("\n", strip=True).replace(c_name, "", 1).strip()
            lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
            if lines:
                process_line(c_name, lines[0])

    return notes_map


def parse_courses_catalog_html(
    html_content: str,
    announcement_notes: Optional[Dict[str, Dict[Tuple[int, int], Tuple[str, bool]]]] = None,
) -> Dict[str, CourseCatalogItem]:
    """
    解析 `https://selectcourse.cgbf.nccu.edu.tw/courses` (課程資料) 頁面。
    擷取每門課的科目代碼、名稱、授課教師、備註（含教室）與 `<ul class="ul-timetable">` 中的每一堂課確切日期與時段。
    回傳以 `course_code` (及 `norm_name`) 為 key 的字典。
    """
    soup = BeautifulSoup(html_content, "html.parser")
    catalog: Dict[str, CourseCatalogItem] = {}

    for item_div in soup.find_all("div", class_="course-item"):
        fields: Dict[str, str] = {}
        for col in item_div.find_all("div", recursive=True):
            lbl = col.find("label", class_="tt")
            if not lbl:
                continue
            key = lbl.get_text(strip=True)
            p_val = col.find("p")
            if p_val:
                # 排除 badge 文字（如「英文授課」）對課程名稱的干擾
                if key == "科目名稱":
                    badge = p_val.find("span", class_="badge")
                    if badge:
                        badge.extract()
                fields[key] = p_val.get_text("\n", strip=True)

        code = fields.get("科目代碼", "").strip()
        name = fields.get("科目名稱", "").strip()
        if not code and not name:
            continue

        remark = fields.get("備註", "").strip()
        classroom = ""
        room_match = re.search(r"教室[：:]\s*([^\s，,。<\n]+)", remark)
        if room_match:
            classroom = room_match.group(1).strip()

        sessions: List[CourseSession] = []
        norm_name = normalize_course_name(name)
        ann_for_course = (announcement_notes or {}).get(norm_name, {})

        ul_tt = item_div.find("ul", class_="ul-timetable")
        if ul_tt:
            for li in ul_tt.find_all("li"):
                li_text = li.get_text(" ", strip=True)
                # 格式範例：2026-08-31(一) 18:30～21:30
                m = re.search(
                    r"(\d{4})-(\d{2})-(\d{2})(?:\([^)]*\))?\s*(\d{1,2}:\d{2})\s*[~～\-－—至]\s*(\d{1,2}:\d{2})",
                    li_text,
                )
                if m:
                    yr, mo, dy = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    st, et = m.group(4).zfill(5), m.group(5).zfill(5)
                    dt = datetime.date(yr, mo, dy)
                    note, is_cancelled = ann_for_course.get((mo, dy), ("", False))
                    sessions.append(
                        CourseSession(
                            date=dt,
                            start_time=st,
                            end_time=et,
                            note=note,
                            is_cancelled=is_cancelled,
                        )
                    )

        cat_item = CourseCatalogItem(
            course_code=code,
            course_name=name,
            teacher=fields.get("授課教師", ""),
            credits=fields.get("學分數", ""),
            category=fields.get("課程類別", ""),
            classroom=classroom,
            remark=remark,
            sessions=sessions,
        )
        if code:
            catalog[code] = cat_item
        if norm_name:
            catalog[norm_name] = cat_item

    return catalog


def parse_my_enrollments_html(html_content: str) -> List[EnrolledCourseItem]:
    """
    解析 `https://selectcourse.cgbf.nccu.edu.tw/enrollments` (我的課程) 頁面。
    回傳使用者選修的所有課程清單。
    """
    soup = BeautifulSoup(html_content, "html.parser")
    enrolled_list: List[EnrolledCourseItem] = []

    for item_div in soup.find_all("div", class_="course-item"):
        fields: Dict[str, str] = {}
        for col in item_div.find_all("div", recursive=True):
            lbl = col.find("label", class_="tt")
            if not lbl:
                continue
            key = lbl.get_text(strip=True)
            p_val = col.find("p")
            if p_val:
                if key == "科目名稱":
                    badge = p_val.find("span", class_="badge")
                    if badge:
                        badge.extract()
                fields[key] = p_val.get_text(" ", strip=True)

        code = fields.get("科目代碼", "").strip()
        name = fields.get("科目名稱", "").strip()
        if not code and not name:
            continue

        enrolled_list.append(
            EnrolledCourseItem(
                course_code=code,
                course_name=name,
                teacher=fields.get("授課教師", ""),
                credits=fields.get("學分數", ""),
                category=fields.get("課程類別", ""),
                status=fields.get("修課狀態", ""),
            )
        )

    return enrolled_list


def generate_ics_from_catalog(
    enrolled_courses: List[EnrolledCourseItem],
    catalog: Dict[str, CourseCatalogItem],
    include_cancelled: bool = False,
) -> Tuple[str, int]:
    """根據我的課程與課程資料的時間表，產生 RFC 5545 標準 `.ics` 檔案內容"""
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//NCCU CGBF Course Calendar Exporter//ZH-TW",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:政大國際金融學院課表",
        "X-WR-TIMEZONE:Asia/Taipei",
        "BEGIN:VTIMEZONE",
        "TZID:Asia/Taipei",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:+0800",
        "TZOFFSETTO:+0800",
        "TZNAME:CST",
        "DTSTART:19700101T000000",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]

    event_count = 0

    for item in enrolled_courses:
        cat = catalog.get(item.course_code) or catalog.get(normalize_course_name(item.course_name))
        if not cat or not cat.sessions:
            continue

        total_active = len([s for s in cat.sessions if not s.is_cancelled])
        active_idx = 0

        for sess in cat.sessions:
            if sess.is_cancelled and not include_cancelled:
                continue
            if not sess.is_cancelled:
                active_idx += 1

            sh, sm = [int(x) for x in sess.start_time.split(":")]
            eh, em = [int(x) for x in sess.end_time.split(":")]
            dt_start = datetime.datetime(sess.date.year, sess.date.month, sess.date.day, sh, sm)
            dt_end = datetime.datetime(sess.date.year, sess.date.month, sess.date.day, eh, em)

            summary = item.course_name
            if sess.is_cancelled:
                summary = f"[停課/取消] {summary}"
            elif sess.note:
                summary = f"{summary} ({sess.note})"

            desc_parts = [
                f"科目代碼：{item.course_code}",
                f"課程名稱：{item.course_name} ({item.category} / {item.credits}學分)",
                f"授課教師：{item.teacher}",
                f"堂次：第 {active_idx}/{total_active} 堂",
            ]
            if cat.remark:
                remark_clean = cat.remark.replace("\r\n", "\\n").replace("\n", "\\n")
                desc_parts.append(f"課程備註：{remark_clean}")

            description = "\\n".join(desc_parts)
            uid = f"{uuid.uuid5(uuid.NAMESPACE_DNS, f'{item.course_code}-{sess.date.isoformat()}-{sess.start_time}')}@cgbf.nccu.edu.tw"

            lines.extend(
                [
                    "BEGIN:VEVENT",
                    f"UID:{uid}",
                    f"DTSTAMP:{now_utc}",
                    f"DTSTART;TZID=Asia/Taipei:{dt_start.strftime('%Y%m%dT%H%M%S')}",
                    f"DTEND;TZID=Asia/Taipei:{dt_end.strftime('%Y%m%dT%H%M%S')}",
                    f"SUMMARY:{summary}",
                    f"DESCRIPTION:{description}",
                    "END:VEVENT",
                ]
            )
            event_count += 1

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n", event_count


def send_ics_via_gmail(
    sender_email: str,
    app_password: str,
    ics_path: str,
    txt_path: str,
    report_text: str,
    event_count: int,
) -> bool:
    """使用者的 Gmail 帳號透過 SMTP (smtp.gmail.com:465) 將 .ics 與課表清單寄給自己"""
    if not sender_email or not app_password:
        return False

    # 移除 Google 應用程式密碼常見的空白字元 (例如 "abcd efgh ijkl mnop" -> "abcdefghijklmnop")
    clean_password = re.sub(r"\s+", "", app_password)

    print(f"\n📧 正在透過 Gmail ({sender_email}) 將 .ics 行事曆寄給您自己...")
    msg = EmailMessage()
    msg["Subject"] = f"🎓 政大國際金融學院 - 個人課表與行事曆 ({event_count} 堂課)"
    msg["From"] = sender_email
    msg["To"] = sender_email

    body = (
        "您好！\n\n"
        "附件為自動從「國立政治大學國際金融學院選課系統」擷取並對照完成的個人行事曆 (.ics) 與課表文字清單。\n"
        "您可以直接在手機或電腦的信件中點開附件 `nccu_cgbf_courses.ics`，即可一鍵加入 Google 日曆、Apple 行事曆或 Outlook！\n\n"
        + report_text
    )
    msg.set_content(body)

    if os.path.exists(ics_path):
        with open(ics_path, "rb") as f:
            msg.add_attachment(
                f.read(),
                maintype="text",
                subtype="calendar",
                filename=os.path.basename(ics_path),
            )

    if os.path.exists(txt_path):
        with open(txt_path, "rb") as f:
            msg.add_attachment(
                f.read(),
                maintype="text",
                subtype="plain",
                filename=os.path.basename(txt_path),
            )

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(sender_email, clean_password)
            server.send_message(msg)
        print(f"✅ 寄信成功！已將 `{os.path.basename(ics_path)}` 寄送至 {sender_email}！")
        return True
    except smtplib.SMTPAuthenticationError:
        print(
            "❌ Gmail 登入驗證失敗！\n"
            "💡 提醒：Gmail 透過程式寄信必須使用 16 位數的「Google 應用程式密碼 (App Password)」，不能直接使用一般登入密碼。\n"
            "👉 快速申請網址：https://myaccount.google.com/apppasswords"
        )
    except Exception as exc:
        print(f"❌ 寄信時發生錯誤：{exc}")
    return False


def format_and_save_outputs(
    enrolled_courses: List[EnrolledCourseItem],
    catalog: Dict[str, CourseCatalogItem],
    output_ics_path: str,
    email: Optional[str] = None,
    email_password: Optional[str] = None,
) -> None:
    """在終端機印出完整的個人課表時間，並儲存文字檔與 `.ics` 行事曆檔"""
    report_lines = []

    def log(text: str = ""):
        print(text)
        report_lines.append(text)

    log("\n" + "=" * 72)
    log(f"🎓 【您的個人選修課程與上課時間總表】（共選修 {len(enrolled_courses)} 門課）")
    log("=" * 72)

    all_sessions_chronological = []

    for idx, item in enumerate(enrolled_courses, start=1):
        cat = catalog.get(item.course_code) or catalog.get(normalize_course_name(item.course_name))
        log(
            f"\n📘 {idx}. [{item.course_code}] {item.course_name} "
            f"({item.category} {item.credits}學分 | 👨‍🏫 教師：{item.teacher})"
        )

        if not cat or not cat.sessions:
            log("   ⚠️ 系統顯示無固定上課時間或未指定")
            continue

        active_sessions = [s for s in cat.sessions if not s.is_cancelled]
        cancelled_sessions = [s for s in cat.sessions if s.is_cancelled]

        log(f"   ✅ 上課時間（共 {len(active_sessions)} 堂）：")
        for s_idx, sess in enumerate(active_sessions, start=1):
            wd = WEEKDAY_NAMES[sess.date.weekday()]
            note_s = f" 📌 {sess.note}" if sess.note else ""
            log(f"      ({s_idx:2d}) {sess.date.strftime('%Y-%m-%d')} ({wd}) {sess.start_time}～{sess.end_time}{note_s}")
            all_sessions_chronological.append((sess.date, sess.start_time, sess.end_time, item, cat, sess.note))

        if cancelled_sessions:
            cancel_str = ", ".join(
                f"{s.date.strftime('%Y-%m-%d')}({WEEKDAY_NAMES[s.date.weekday()]}) [{s.note}]"
                for s in cancelled_sessions
            )
            log(f"   ❌ 公告停課/取消：{cancel_str}")

    # 依日期與時間排序的總表
    all_sessions_chronological.sort(key=lambda x: (x[0], x[1]))
    log("\n" + "-" * 72)
    log(f"🗓️ 【依日期先後排序的上課行事曆清單】（共 {len(all_sessions_chronological)} 堂課）")
    log("-" * 72)

    current_month = None
    for dt, st, et, item, cat, note in all_sessions_chronological:
        ym = dt.strftime("%Y年%m月")
        if ym != current_month:
            current_month = ym
            log(f"\n【{ym}】")
        wd = WEEKDAY_NAMES[dt.weekday()]
        note_s = f" ({note})" if note else ""
        log(f"  • {dt.strftime('%Y-%m-%d')} ({wd}) {st}～{et}  |  {item.course_name}{note_s}")

    txt_path = os.path.splitext(output_ics_path)[0] + "_課表時間清單.txt"
    with open(txt_path, "w", encoding="utf-8") as tf:
        tf.write("\n".join(report_lines) + "\n")

    ics_content, event_count = generate_ics_from_catalog(enrolled_courses, catalog)
    with open(output_ics_path, "w", encoding="utf-8", newline="") as f:
        f.write(ics_content)

    print("\n" + "=" * 72)
    print(f"🎉 匯出完成！共為您建立 {event_count} 筆行事曆課程事件。")
    print(f"📄 課表文字總表：{os.path.abspath(txt_path)}")
    print(f"📅 行事曆 (.ics)：{os.path.abspath(output_ics_path)}")
    print("=" * 72)

    sync_courses_to_web_html(catalog, enrolled_courses)
    generate_mobile_qr_download(output_ics_path)

    if email and email_password:
        send_ics_via_gmail(
            sender_email=email,
            app_password=app_password if (app_password := email_password) else "",
            ics_path=output_ics_path,
            txt_path=txt_path,
            report_text="\n".join(report_lines),
            event_count=event_count,
        )


def sync_courses_to_web_html(
    catalog: Dict[str, CourseCatalogItem],
    enrolled_courses: List[EnrolledCourseItem],
) -> None:
    """每次抓取最新選課系統資料時，自動把最新學期的所有課程與時間同步寫入 index.html 網頁版中"""
    web_path = "index.html"
    if not os.path.exists(web_path):
        return
    try:
        unique = []
        seen = set()
        for v in catalog.values():
            if v.course_code and v.course_code not in seen:
                seen.add(v.course_code)
                unique.append(
                    {
                        "code": v.course_code,
                        "name": v.course_name,
                        "teacher": v.teacher,
                        "credits": v.credits,
                        "category": v.category,
                        "sessions": [
                            {
                                "date": s.date.isoformat(),
                                "start": s.start_time,
                                "end": s.end_time,
                                "note": s.note,
                                "cancelled": s.is_cancelled,
                            }
                            for s in v.sessions
                        ],
                    }
                )
        cat_order = {"必修": 0, "群修": 1, "選修": 2}
        unique.sort(
            key=lambda c: (
                0 if c["sessions"] else 1,
                cat_order.get(c["category"], 9),
                c["code"],
            )
        )
        new_json = json.dumps(unique, ensure_ascii=False)
        with open(web_path, "r", encoding="utf-8") as f:
            html = f.read()
        updated_html = re.sub(
            r"const DEFAULT_COURSES = \[.*?\];",
            lambda _: f"const DEFAULT_COURSES = {new_json};",
            html,
            count=1,
            flags=re.DOTALL,
        )
        if enrolled_courses:
            my_codes = json.dumps([c.course_code for c in enrolled_courses if c.course_code], ensure_ascii=False)
            updated_html = re.sub(
                r"const MY_DEFAULT_CODES = \[.*?\];",
                lambda _: f"const MY_DEFAULT_CODES = {my_codes};",
                updated_html,
                count=1,
            )
        with open(web_path, "w", encoding="utf-8") as f:
            f.write(updated_html)
        print(f"🌐 已同步更新網頁版 `{web_path}` 的最新學期課程資料庫（共 {len(unique)} 門課）！")
    except Exception as exc:
        print(f"⚠️ 同步更新 index.html 時發生例外: {exc}")


def generate_mobile_qr_download(ics_path: str) -> Optional[str]:
    """
    將產生的 .ics 檔案產生一個 24 小時有效的臨時 HTTPS 下載連結，並直接在終端機印出 QR Code，
    讓使用者免登入信箱、免設定密碼，直接拿手機相機一掃就能把行事曆加進手機！
    """
    import qrcode

    print("\n📱 正在為您產生手機掃描專用的 QR Code 下載連結（免登入信箱）...")
    download_url = None
    try:
        out = subprocess.check_output(
            [
                "curl.exe",
                "-s",
                "-F",
                "reqtype=fileupload",
                "-F",
                "time=24h",
                "-F",
                f"fileToUpload=@{os.path.abspath(ics_path)}",
                "https://litterbox.catbox.moe/resources/internals/api.php",
            ],
            text=True,
            timeout=20,
        ).strip()
        if out.startswith("https://"):
            download_url = out
    except Exception as exc:
        print(f"⚠️ 無法建立雲端臨時連結: {exc}")

    if download_url:
        print(f"🔗 手機直接下載網址 (24小時內有效)：{download_url}")
        print("👇 請直接拿起手機開啟「相機」掃描下方 QR Code，即可一鍵匯入手機行事曆：\n")
        qr = qrcode.QRCode(border=1)
        qr.add_data(download_url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        print("\n💡 手機操作提示：")
        print("   - iPhone (iOS)：用相機掃描上方 QR Code -> 點開連結 -> 點擊「加入全部 (Add All)」即完成！")
        print("   - Android：掃描後下載 .ics 檔 -> 點擊打開檔案 -> 選擇「Google 日曆」即可匯入！")
    return download_url


def launch_edge_with_cdp_and_connect(p, resolved_ip: str, port: int = 9222, fresh_session: bool = False):
    """
    直接使用原生 msedge.exe 啟動獨立設定檔視窗（或連線至已開啟的 9222 埠），
    徹底避開 Cloudflare Turnstile 對 navigator.webdriver 與自動化特徵的偵測。
    """
    if not fresh_session:
        # 若已經有開著的 9222 視窗，直接連線重用已登入的 Session！
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            return browser, True, None
        except Exception:
            pass

    edge_paths = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    exe_path = next((path for path in edge_paths if os.path.exists(path)), None)
    profile_suffix = f"_{int(time.time())}" if fresh_session else ""
    user_data_dir = os.path.join(os.environ.get("TEMP", "."), f"cgbf_edge_profile{profile_suffix}")

    if exe_path:
        cmd = [
            exe_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            f"--host-resolver-rules=MAP {LOGIN_HOST} {resolved_ip}",
            "--window-position=-32000,-32000",
            "--window-size=1280,860",
            "--no-first-run",
            "--no-default-browser-check",
            LOGIN_URL,
        ]
        # 優先將瀏覽器視窗隱藏或最小化在背景執行，不干擾使用者畫面
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 7  # SW_SHOWMINNOACTIVE (背景最小化不搶焦點)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=startupinfo,
        )
        for _ in range(25):
            try:
                browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                return browser, True, proc
            except Exception:
                time.sleep(0.4)

    launch_args = [
        f"--host-resolver-rules=MAP {LOGIN_HOST} {resolved_ip}",
        "--disable-blink-features=AutomationControlled",
    ]
    browser = p.chromium.launch(
        channel="msedge",
        headless=False,
        args=launch_args,
        ignore_default_args=["--enable-automation"],
    )
    return browser, False, None


def run_browser_workflow(
    output_ics_path: str,
    account: Optional[str] = None,
    password: Optional[str] = None,
    email: Optional[str] = None,
    email_password: Optional[str] = None,
    fresh_session: bool = False,
) -> None:
    from playwright.sync_api import sync_playwright

    print("=" * 72)
    print("🎓 政大國際金融學院 (CGBF) 選課時間自動擷取與行事曆匯出工具")
    print("=" * 72)

    config_file = "user_config.json"
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as cf:
                cfg = json.load(cf)
                account = account or cfg.get("account")
                password = password or cfg.get("password")
                email = email or cfg.get("email")
                email_password = email_password or cfg.get("email_password")
        except Exception:
            pass

    if fresh_session or not account:
        account = account or input("\n👤 請輸入您的學號 (帳號): ").strip()
    if fresh_session or not password:
        if not password:
            password = getpass.getpass("🔑 請輸入您的選課系統密碼 (輸入時不會顯示字元，輸完請按 Enter): ").strip()
            if not password:
                password = input("🔑 請輸入您的選課系統密碼: ").strip()

    resolved_ip = resolve_host_via_public_dns(LOGIN_HOST)
    port = 9224 if fresh_session else 9222

    with sync_playwright() as p:
        print(f"\n[步驟 1/4] 正在啟動瀏覽器並連線至選課系統...")
        browser, is_cdp, edge_proc = launch_edge_with_cdp_and_connect(
            p, resolved_ip, port=port, fresh_session=fresh_session
        )

        if is_cdp:
            context = browser.contexts[0]
            for _ in range(20):
                if context.pages:
                    break
                time.sleep(0.3)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                cdp_sess = context.new_cdp_session(page)
                win_info = cdp_sess.send("Browser.getWindowForTarget")
                cdp_sess.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": win_info["windowId"],
                        "bounds": {"left": -32000, "top": -32000, "width": 1280, "height": 860},
                    },
                )
            except Exception:
                pass
        else:
            context = browser.new_context(viewport={"width": 1280, "height": 860})
            page = context.new_page()

        # 先前往首頁檢查登入狀態並取得首頁課程異動公告
        page.goto(BASE_URL, wait_until="domcontentloaded")
        time.sleep(0.8)
        home_or_login_html = page.content()
        announcement_notes = parse_announcement_cancellations(home_or_login_html)

        # 若被導向 /login，代表尚未登入，詢問帳密並自動填入登入
        if "/login" in page.url:
            config_file = "user_config.json"
            if not account or not password:
                if os.path.exists(config_file):
                    try:
                        with open(config_file, "r", encoding="utf-8") as cf:
                            cfg = json.load(cf)
                            account = account or cfg.get("account")
                            password = password or cfg.get("password")
                    except Exception:
                        pass

            if not account:
                account = input("\n👤 請輸入您的學號 (帳號): ").strip()
            if not password:
                password = getpass.getpass("🔑 請輸入您的密碼 (輸入時不會顯示字元，輸完請按 Enter): ").strip()
                if not password:
                    password = input("🔑 請輸入您的密碼: ").strip()

            print("\n[步驟 2/4] 正在自動勾選學習規範並填入您的帳號密碼...")
            page.evaluate(
                """({acc, pwd}) => {
                    const expireTime = Date.now() + 86400 * 1000;
                    localStorage.setItem('cgbf_agreement_expires_at', expireTime.toString());
                    document.querySelectorAll('#agreement-modal input[name="agreement_items[]"]').forEach(el => {
                        el.checked = true;
                    });
                    const loginAgree = document.querySelector('#login-agreement');
                    if (loginAgree) loginAgree.checked = true;

                    const accInput = document.querySelector('#account, input[name="account"]');
                    if (accInput && acc) {
                        accInput.value = acc;
                        accInput.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                    const pwdInput = document.querySelector('#password, input[name="password"]');
                    if (pwdInput && pwd) {
                        pwdInput.value = pwd;
                        pwdInput.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                }""",
                {"acc": account, "pwd": password},
            )
            print("⏳ 正在背景自動通過 Cloudflare 驗證並登入中，請稍候...")

            for i in range(180):
                if "/login" not in page.url:
                    break
                try:
                    token_ready = page.evaluate(
                        """() => {
                            const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
                            return cfInput && cfInput.value && cfInput.value.length > 10;
                        }"""
                    )
                    if token_ready:
                        print("✅ 背景驗證已通過，正在自動登入...")
                        submit_btn = page.query_selector('button[name="btn_login"], button[type="submit"]')
                        if submit_btn:
                            submit_btn.click()
                        page.wait_for_load_state("domcontentloaded", timeout=10000)
                        time.sleep(1.2)
                        if "/login" not in page.url:
                            break
                    elif i == 24 and is_cdp:
                        # 若超過 12 秒 Cloudflare 強制要求真人手動勾選，才將視窗移回畫面中央讓使用者點一下
                        print("💡 Cloudflare 要求手動點擊一下驗證框，正在為您顯示驗證畫面...")
                        cdp_sess = context.new_cdp_session(page)
                        win_info = cdp_sess.send("Browser.getWindowForTarget")
                        cdp_sess.send(
                            "Browser.setWindowBounds",
                            {
                                "windowId": win_info["windowId"],
                                "bounds": {"left": 100, "top": 80, "width": 1100, "height": 800, "windowState": "normal"},
                            },
                        )
                except Exception:
                    pass
                time.sleep(0.5)

            if "/login" in page.url:
                input("⚠️ 請在瀏覽器確認完成登入後，按 [Enter] 繼續...")

            # 登入後再抓一次首頁公告確保最新
            home_or_login_html = page.content()
            announcement_notes = parse_announcement_cancellations(home_or_login_html)
        else:
            print("✅ 偵測到瀏覽器已是登入狀態，直接進行擷取！")

        print(f"\n[步驟 3/4] 正在抓取「我的課程 ({ENROLLMENTS_URL})」與「課程資料 ({COURSES_URL})」...")
        page.goto(ENROLLMENTS_URL, wait_until="domcontentloaded")
        time.sleep(0.8)
        enrollments_html = page.content()
        with open("debug_enrollments.html", "w", encoding="utf-8") as f:
            f.write(enrollments_html)

        page.goto(COURSES_URL, wait_until="domcontentloaded")
        time.sleep(0.8)
        courses_html = page.content()
        with open("debug_courses.html", "w", encoding="utf-8") as f:
            f.write(courses_html)

        print("🔒 資料擷取完畢，正在自動關閉瀏覽器...")
        try:
            for ctx in list(browser.contexts):
                for pg in list(ctx.pages):
                    try:
                        pg.close()
                    except Exception:
                        pass
            browser.close()
        except Exception:
            pass
        if edge_proc:
            try:
                edge_proc.terminate()
            except Exception:
                pass

    enrolled_courses = parse_my_enrollments_html(enrollments_html)
    catalog = parse_courses_catalog_html(courses_html, announcement_notes)

    format_and_save_outputs(
        enrolled_courses,
        catalog,
        output_ics_path,
        email=email,
        email_password=email_password,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="國立政治大學國際金融學院 (CGBF) 選課系統課表擷取與 .ics 行事曆匯出工具"
    )
    parser.add_argument(
        "-o",
        "--output",
        default="nccu_cgbf_courses.ics",
        help="輸出的 .ics 檔案路徑 (預設: nccu_cgbf_courses.ics)",
    )
    parser.add_argument("-u", "--account", help="選課系統帳號（學號）")
    parser.add_argument("-p", "--password", help="選課系統密碼")
    parser.add_argument("-e", "--email", help="用來寄送 .ics 給自己的 Gmail 信箱")
    parser.add_argument("--email-password", help="Gmail 的 16 位數 Google 應用程式密碼 (App Password)")
    parser.add_argument(
        "--fresh",
        action="store_true",
        default=True,
        help="強制使用全新未登入的瀏覽器設定檔從頭執行（預設開啟）",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="離線模式：直接讀取已下載的 debug_enrollments.html 與 debug_courses.html 產生課表",
    )

    args = parser.parse_args()

    if args.offline and os.path.exists("debug_enrollments.html") and os.path.exists("debug_courses.html"):
        ann_notes = {}
        if os.path.exists("debug_logged_in_page.html"):
            with open("debug_logged_in_page.html", "r", encoding="utf-8") as f:
                ann_notes = parse_announcement_cancellations(f.read())
        with open("debug_enrollments.html", "r", encoding="utf-8") as f:
            enrolled = parse_my_enrollments_html(f.read())
        with open("debug_courses.html", "r", encoding="utf-8") as f:
            catalog = parse_courses_catalog_html(f.read(), ann_notes)
        format_and_save_outputs(
            enrolled,
            catalog,
            args.output,
            email=args.email,
            email_password=args.email_password,
        )
        return

    run_browser_workflow(
        output_ics_path=args.output,
        account=args.account,
        password=args.password,
        email=args.email,
        email_password=args.email_password,
        fresh_session=args.fresh,
    )


if __name__ == "__main__":
    import sys
    try:
        main()
    except Exception as exc:
        print(f"\n❌ 發生錯誤：{exc}")
    if getattr(sys, "frozen", False):
        input("\n👉 請按 [Enter] 鍵關閉視窗...")
