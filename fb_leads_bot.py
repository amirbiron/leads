"""
Facebook Leads Finder Bot - v0.2
================================
זרימה: Facebook Notifications → Email (Gmail/Outlook) → AI Analysis → Telegram Alert

מבוסס על ארכיטקטורה מינימלית:
פייסבוק שולח התראות למייל → הבוט קורא מיילים → מסנן עם מילות מפתח →
שולח ל-AI לניתוח → אם רלוונטי ו-confidence >= 80 → שולח התראה לטלגרם

שיפורים לפי ביקורות:
- חילוץ HTML עם BeautifulSoup (לא רק text/plain)
- חילוץ Subject ממייל (לפעמים המידע העיקרי שם)
- Pre-filter "טיפש" עם מילות מפתח (חיסכון בעלויות AI)
- פלט AI בפורמט JSON מובנה (לא בדיקת "כן" בטקסט חופשי)
- סף confidence >= 80 לשליחת התראה
- Fingerprint dedup למניעת כפילויות (hash של subject+snippet)
- שימוש ב-IMAP UID (מזהה יציב) במקום IDs רציפים
- סימון Seen גם במקרה של כשל AI/כשל עיבוד (כדי למנוע לולאה אינסופית)
- הסרת מידע אישי (טלפונים/מיילים) לפני שליחה ל-AI
- משתני סביבה לכל הסודות
- Error handling מלא עם retry logic
- לוגים עם timestamp
"""

import imaplib
import email
import requests
import time
import json
import os
import re
import hashlib
from datetime import datetime
from email.header import decode_header
from bs4 import BeautifulSoup
from openai import OpenAI

# ──────────────────────────────────────────────
# קונפיגורציה - הכל דרך משתני סביבה
# ──────────────────────────────────────────────
# IMAP endpoint
# אם לא מגדירים IMAP_SERVER, ננסה להסיק לפי הדומיין של EMAIL_USER (gmail/outlook).
EMAIL_USER = os.getenv("EMAIL_USER", "").strip()
IMAP_PROVIDER = os.getenv("IMAP_PROVIDER", "auto").lower().strip()  # auto | gmail | outlook
_IMAP_SERVER_ENV = os.getenv("IMAP_SERVER", "").strip()
_IMAP_PORT_ENV = os.getenv("IMAP_PORT", "").strip()


def _guess_provider_from_email(addr: str) -> str | None:
    if "@" not in (addr or ""):
        return None
    domain = addr.split("@", 1)[1].lower().strip()
    if domain in ("gmail.com", "googlemail.com"):
        return "gmail"
    if domain in ("outlook.com", "hotmail.com", "live.com", "msn.com"):
        return "outlook"
    return None


def _guess_provider_from_host(host: str) -> str | None:
    h = (host or "").lower()
    if "gmail" in h:
        return "gmail"
    if "office365" in h or "outlook" in h:
        return "outlook"
    return None


def _resolve_imap_endpoint() -> tuple[str, int, str]:
    provider_pref = IMAP_PROVIDER if IMAP_PROVIDER else "auto"
    provider_email = _guess_provider_from_email(EMAIL_USER)
    provider_host = _guess_provider_from_host(_IMAP_SERVER_ENV)

    # Resolve host
    host = _IMAP_SERVER_ENV
    if not host:
        # Prefer explicit provider, otherwise infer from email.
        inferred = provider_email
        if provider_pref == "gmail" or inferred == "gmail":
            host = "imap.gmail.com"
        elif provider_pref == "outlook" or inferred == "outlook":
            host = "outlook.office365.com"
        else:
            # Backward-compatible fallback
            host = "outlook.office365.com"

    # Resolve port (default 993 for both Gmail/Outlook IMAP SSL)
    try:
        port = int(_IMAP_PORT_ENV) if _IMAP_PORT_ENV else 0
    except Exception:
        port = 0
    if not port:
        port = 993

    # Resolve provider label (used for auth decisions + better logs)
    provider = _guess_provider_from_host(host) or provider_host or provider_email or "unknown"
    return host, port, provider


IMAP_SERVER, IMAP_PORT, IMAP_PROVIDER_RESOLVED = _resolve_imap_endpoint()

# Basic auth password (עובד ב-Gmail עם App Password; ב-Outlook לרוב חסום)
EMAIL_PASS = os.getenv("EMAIL_PASS", "").strip()

# IMAP auth method: basic | xoauth2 | auto
# - basic: משתמש ב-EMAIL_PASS
# - xoauth2: משתמש ב-refresh token של Microsoft (ל-Outlook/Office365)
# - auto: אם יש MS_REFRESH_TOKEN+MS_CLIENT_ID ינסה xoauth2, אחרת basic
IMAP_AUTH_METHOD = os.getenv("IMAP_AUTH_METHOD", "auto").lower().strip()

# Microsoft OAuth2 (ל-Outlook/Office365 IMAP via XOAUTH2)
# ברוב המקרים עם חשבון Outlook.com אישי: MS_TENANT="consumers"
MS_TENANT = os.getenv("MS_TENANT", "consumers").strip()
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "").strip()
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET", "").strip()
MS_REFRESH_TOKEN = os.getenv("MS_REFRESH_TOKEN", "").strip()
# Scope ברירת מחדל ל-IMAP. ניתן להוסיף גם SMTP.Send אם תרצה לשלוח מיילים בעתיד.
MS_SCOPE = os.getenv(
    "MS_SCOPE",
    "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
).strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# הגדרות מתקדמות
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))  # שניות בין בדיקות
CONFIDENCE_THRESHOLD = int(os.getenv("CONFIDENCE_THRESHOLD", "80"))
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
MAX_POST_LENGTH = 2000  # חיתוך טקסט לפני שליחה ל-AI
ERROR_MAILBOX = os.getenv("ERROR_MAILBOX", "")  # לדוגמה: "Error" / "Errors" / "FB Errors"

# מילות מפתח לסינון ראשוני (חוסך עלויות AI)
TRIGGER_KEYWORDS = [
    # עברית
    "בוט", "אוטומציה", "סקריפט", "מתכנת", "פיתוח", "מחפש מפתח",
    "דרוש מפתח", "צריך עזרה", "מישהו יכול", "לבנות מערכת",
    "טלגרם", "אינטגרציה", "אוטומטי", "ווטסאפ", "בהתנדבות",
    # אנגלית
    "bot", "telegram", "whatsapp", "automation", "python", "script",
    "developer", "zapier", "make", "integromat", "api",
]

# מילות מפתח לסינון הודעות מערכת של פייסבוק (Negative keywords)
IGNORE_KEYWORDS = [
    "הצטרפת לקבוצה",
    "התחברת הרגע",
    "התראת אבטחה",
    "security alert",
    "new login",
    "reset your password",
    "ברוך הבא לקבוצה",
]

# Fingerprints - למניעת כפילויות (בזיכרון, מספיק להובי)
# Fingerprints - למניעת כפילויות (שומר על סדר ההכנסה)
seen_fingerprints: dict[str, bool] = {}
MAX_FINGERPRINTS = 5000  # שומר את האחרונים בלבד

# ──────────────────────────────────────────────
# כלי עזר
# ──────────────────────────────────────────────

client = OpenAI(api_key=OPENAI_API_KEY)


def log(msg: str):
    """לוג עם timestamp"""
    # חשוב ל-Render/containers: stdout הוא לא TTY ולכן Python עלול לא לעשות flush מיידי.
    # flush=True מבטיח שתראה לוגים בזמן אמת.
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _ms_token_endpoint() -> str:
    return f"https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0/token"


def get_ms_access_token() -> str | None:
    """מחלץ access token מ-Microsoft בעזרת refresh token (OAuth2 v2)."""
    if not (MS_CLIENT_ID and MS_REFRESH_TOKEN):
        return None

    data = {
        "client_id": MS_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": MS_REFRESH_TOKEN,
        "scope": MS_SCOPE,
    }
    if MS_CLIENT_SECRET:
        data["client_secret"] = MS_CLIENT_SECRET

    try:
        resp = requests.post(_ms_token_endpoint(), data=data, timeout=20)
        if not resp.ok:
            # ניסיון להדפיס שגיאה קריאה (אם יש JSON)
            try:
                err = resp.json()
            except Exception:
                err = resp.text
            log(f"⚠️ OAuth2 token נכשל ({resp.status_code}): {err}")
            return None
        payload = resp.json()
        return payload.get("access_token")
    except Exception as e:
        log(f"⚠️ שגיאת רשת בקבלת OAuth2 token: {e}")
        return None


def connect_imap():
    """יוצר חיבור IMAP מחובר (basic או XOAUTH2)."""
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)

    method = IMAP_AUTH_METHOD
    if method not in ("basic", "xoauth2", "oauth2", "auto"):
        method = "auto"

    ms_oauth_supported = IMAP_PROVIDER_RESOLVED in ("outlook", "office365", "microsoft")

    if method in ("xoauth2", "oauth2"):
        if not ms_oauth_supported:
            raise imaplib.IMAP4.error(
                "XOAUTH2 is only implemented for Microsoft (Outlook/Office365) in this bot. "
                "For Gmail use IMAP_AUTH_METHOD=basic with an App Password, and IMAP_SERVER=imap.gmail.com."
            )
        want_oauth = True
    elif method == "auto":
        want_oauth = ms_oauth_supported and MS_CLIENT_ID and MS_REFRESH_TOKEN
    else:
        want_oauth = False

    log(f"🔌 IMAP connect: {IMAP_SERVER}:{IMAP_PORT} (provider={IMAP_PROVIDER_RESOLVED}, auth={'xoauth2' if want_oauth else 'basic'})")

    if want_oauth:
        token = get_ms_access_token()
        if not token:
            raise imaplib.IMAP4.error("OAuth2 token acquisition failed (missing/invalid token)")

        auth_string = f"user={EMAIL_USER}\x01auth=Bearer {token}\x01\x01".encode("utf-8")
        mail.authenticate("XOAUTH2", lambda _: auth_string)
    else:
        mail.login(EMAIL_USER, EMAIL_PASS)

    return mail


def make_fingerprint(subject: str, snippet: str) -> str:
    """יוצר hash ייחודי לפוסט למניעת כפילויות"""
    raw = f"{subject}|{snippet[:200]}".lower().strip()
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def is_duplicate(fingerprint: str) -> bool:
    """בודק אם כבר טיפלנו בפוסט הזה"""
    global seen_fingerprints
    if fingerprint in seen_fingerprints:
        return True
    seen_fingerprints[fingerprint] = True

    # ניקוי הישנים ביותר אם הרשימה גדלה מדי
    if len(seen_fingerprints) > MAX_FINGERPRINTS:
        old_keys = list(seen_fingerprints.keys())[: MAX_FINGERPRINTS // 2]
        for k in old_keys:
            del seen_fingerprints[k]
    return False


def strip_pii(text: str) -> str:
    """מסיר מספרי טלפון ומיילים מהטקסט לפני שליחה ל-AI"""
    # טלפונים ישראליים ובינלאומיים
    text = re.sub(r'0[0-9]{1,2}[-.\s]?[0-9]{3}[-.\s]?[0-9]{4}', '[טלפון הוסר]', text)
    text = re.sub(r'\+?972[-.\s]?[0-9]{1,2}[-.\s]?[0-9]{3}[-.\s]?[0-9]{4}', '[טלפון הוסר]', text)
    # מיילים
    text = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '[מייל הוסר]', text)
    return text


# ──────────────────────────────────────────────
# שלב 1: קריאת מיילים מ-Outlook
# ──────────────────────────────────────────────

def decode_subject(msg) -> str:
    """מפענח את הנושא של המייל (כולל encoding)"""
    subject = msg.get("Subject", "")
    if not subject:
        return ""
    decoded_parts = decode_header(subject)
    result = ""
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            result += part.decode(charset or "utf-8", errors="ignore")
        else:
            result += part
    return result


def extract_email_body(msg) -> str:
    """מחלץ טקסט נקי מגוף המייל - HTML ואז plain"""
    html_body = ""
    plain_body = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                decoded = payload.decode(errors="ignore")
            except Exception:
                continue

            if content_type == "text/html":
                html_body += decoded
            elif content_type == "text/plain":
                plain_body += decoded
    else:
        try:
            payload = msg.get_payload(decode=True).decode(errors="ignore")
        except Exception:
            return ""
        if msg.get_content_type() == "text/html":
            html_body = payload
        else:
            plain_body = payload

    # עדיפות ל-HTML כי מיילים של פייסבוק בד"כ מלאים יותר שם
    if html_body:
        soup = BeautifulSoup(html_body, "html.parser")
        # הסרת סקריפטים וסטיילים
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
    elif plain_body:
        text = plain_body
    else:
        return ""

    # ניקוי רווחים ושורות ריקות
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    final_text = "\n".join(lines)

    # חיתוך ה-Footer של פייסבוק (חיסכון בטוקנים והפחתת "רעש" ל-AI)
    cutoff_phrases = [
        "View on Facebook",
        "Reply to this email",
        "הצג בפייסבוק",
        "כדי להפסיק לקבל התראות",
    ]
    earliest_cut = None
    for phrase in cutoff_phrases:
        idx = final_text.find(phrase)
        if idx != -1:
            earliest_cut = idx if earliest_cut is None else min(earliest_cut, idx)
    if earliest_cut is not None:
        final_text = final_text[:earliest_cut]

    return final_text.strip()


def _imap_uid_store(mail_conn, uid: str, command: str, flags: str) -> bool:
    """עוזר: STORE לפי UID עם טיפול בשגיאות"""
    try:
        status, _ = mail_conn.uid("STORE", uid, command, flags)
        return status == "OK"
    except Exception as e:
        log(f"⚠️ שגיאת IMAP UID STORE (uid={uid}): {e}")
        return False


def mark_seen(mail_conn, uid: str) -> bool:
    """מסמן מייל כ-Seen לפי UID"""
    return _imap_uid_store(mail_conn, uid, "+FLAGS", "\\Seen")


def flag_error(mail_conn, uid: str) -> bool:
    """מסמן מייל כ-Flagged (עוזר למציאת כשלונות)"""
    return _imap_uid_store(mail_conn, uid, "+FLAGS", "\\Flagged")


def move_to_mailbox(mail_conn, uid: str, mailbox: str) -> bool:
    """
    מעביר מייל לתיבה אחרת לפי UID (COPY + \\Deleted + EXPUNGE).
    אם נכשל, נחזיר False כדי שנוכל לפחות לסמן Seen.
    """
    if not mailbox:
        return False
    try:
        # צריך לצטט שמות תיבות (במיוחד עם רווחים). נשתמש במנגנון הציטוט של imaplib אם קיים.
        try:
            mb = mail_conn._quote(mailbox)
        except Exception:
            mb = mailbox
            if (" " in mb) and not (mb.startswith('"') and mb.endswith('"')):
                mb = f"\"{mb}\""
        # ננסה ליצור את התיבה אם לא קיימת (חלק מהשרתים יחזירו NO אם קיימת)
        try:
            mail_conn.create(mb)
        except Exception:
            pass

        status, _ = mail_conn.uid("COPY", uid, mb)
        if status != "OK":
            return False
        if not _imap_uid_store(mail_conn, uid, "+FLAGS", "\\Deleted"):
            return False
        try:
            mail_conn.expunge()
        except Exception:
            # גם אם expunge נכשל, לפחות סימנו למחיקה
            pass
        return True
    except Exception as e:
        log(f"⚠️ שגיאת העברה לתיבה '{mailbox}' (uid={uid}): {e}")
        return False


def handle_processing_failure(mail_conn, uid: str, subject: str, reason: str):
    """מבטיח שהמייל לא ייתקע כ-UNSEEN במקרה של כשל."""
    subj = (subject or "")[:60]
    log(f"⚠️ כשל בעיבוד (uid={uid}, subject='{subj}'): {reason}")
    # נסמן כ-Flagged כדי שיהיה קל למצוא ידנית
    flag_error(mail_conn, uid)
    # אם הוגדרה תיבת שגיאות - נעביר אליה; אחרת נסמן Seen
    if ERROR_MAILBOX and move_to_mailbox(mail_conn, uid, ERROR_MAILBOX):
        return
    mark_seen(mail_conn, uid)


def fetch_facebook_emails():
    """מתחבר ל-IMAP ומחזיר רשימת מיילים חדשים מפייסבוק"""
    # החיבור עצמו (basic או XOAUTH2) מטופל ב-connect_imap()
    mail = connect_imap()
    mail.select("inbox")

    # משתמשים ב-UID כדי לקבל מזהים יציבים (IMAP message sequence IDs עלולים להשתנות)
    status, data = mail.uid("SEARCH", None, '(UNSEEN FROM "facebookmail.com")')
    uid_list = data[0].split() if (status == "OK" and data and data[0]) else []

    results = []
    for uid_bytes in uid_list:
        uid = uid_bytes.decode(errors="ignore")
        status, msg_data = mail.uid("FETCH", uid, "(BODY.PEEK[])")
        if status != "OK" or not msg_data:
            log(f"⚠️ FETCH נכשל (uid={uid})")
            continue

        raw_bytes = None
        for item in msg_data:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                raw_bytes = item[1]
                break
        if not raw_bytes:
            log(f"⚠️ FETCH החזיר תוכן ריק (uid={uid})")
            continue

        msg = email.message_from_bytes(raw_bytes)

        subject = decode_subject(msg)
        body = extract_email_body(msg)
        full_text = f"{subject}\n\n{body}" if subject else body

        results.append({
            "uid": uid,
            "subject": subject,
            "body": body,
            "full_text": full_text,
        })

    return mail, results


# ──────────────────────────────────────────────
# שלב 2: סינון מהיר (לפני AI)
# ──────────────────────────────────────────────

def quick_keyword_filter(text: str) -> str:
    """מחזיר 'pass' אם תקין, או מחרוזת עם סיבת הפסילה."""
    text_lower = (text or "").lower()

    if any(bad_kw in text_lower for bad_kw in IGNORE_KEYWORDS):
        return "הודעת מערכת/זבל"

    if not any(kw in text_lower for kw in TRIGGER_KEYWORDS):
        return "אין מילות מפתח רלוונטיות"

    return "pass"


# ──────────────────────────────────────────────
# שלב 3: ניתוח AI
# ──────────────────────────────────────────────

AI_PROMPT = """# המשימה שלך: אנליסט הזדמנויות לפרויקטי צד

אתה AI שתפקידו לנתח פוסטים מקבוצות פייסבוק ולזהות הזדמנויות לפרויקטי פיתוח קטנים ובינוניים בתחום הבוטים והאוטומציה.
המטרה: למצוא אנשים פרטיים או עסקים קטנים שמחפשים עזרה.
הפרויקט הוא הובי - גם פניות להתנדבות רלוונטיות.

---
# קריטריונים לסינון:

✅ רלוונטי:
- נושאים: בוטים לטלגרם, בוטים לוואטסאפ, אוטומציות, סקריפטים, פרויקטים קטנים ב-Python
- סוג פנייה: "מחפש/ת", "צריך/ה", "מישהו יכול לעזור", "יש לי רעיון לבוט", "מחפש מתנדב/ת"
- קהל: אנשים פרטיים, סטודנטים, עסקים קטנים, יזמים בתחילת הדרך
- היקף: פרויקטים קטנים-בינוניים, עזרה נקודתית, פרויקטי צד, התנדבות

❌ לא רלוונטי:
- מודעות דרושים למשרה מלאה/חלקית
- אנשים שמפרסמים שהם בונים בוטים (הצעת שירותים)
- שאלות טכניות של מתכנתים על קוד
- עיצוב גרפי, שיווק, בניית אתרים (אלא אם קשור ישירות לאוטומציה)
- דיונים כלליים על טכנולוגיה בלי בקשת עזרה

---
# פורמט פלט - JSON בלבד:

{
  "is_relevant": true/false,
  "confidence": <מספר 0-100>,
  "reason": "<הסבר קצר בעברית>",
  "project_type": "<בוט טלגרם / בוט וואטסאפ / אוטומציה / סקריפט / אחר / לא רלוונטי>",
  "summary": "<תקציר הפוסט ב-2-3 משפטים>",
  "suggested_reply": "<הודעה קצרה ונחמדה שאפשר לשלוח לכותב הפוסט, אם רלוונטי>"
}

---
# פוסט לניתוח:

{{POST_TEXT}}"""


def analyze_with_ai(post_text: str) -> dict | None:
    """שולח פוסט ל-AI ומחזיר ניתוח בפורמט JSON"""
    # הסרת PII לפני שליחה
    clean_text = strip_pii(post_text[:MAX_POST_LENGTH])
    prompt = AI_PROMPT.replace("{{POST_TEXT}}", clean_text)

    try:
        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        result = json.loads(response.choices[0].message.content)
        return result
    except json.JSONDecodeError as e:
        log(f"⚠️ AI החזיר JSON לא תקין: {e}")
        return None
    except Exception as e:
        log(f"⚠️ שגיאת AI: {e}")
        return None


# ──────────────────────────────────────────────
# שלב 4: שליחה לטלגרם
# ──────────────────────────────────────────────

def send_telegram(text: str, parse_mode: str = "HTML"):
    """שולח הודעה לטלגרם"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[:4000],
            "parse_mode": parse_mode,
        }, timeout=10)
        if not resp.ok:
            log(f"⚠️ שגיאת טלגרם: {resp.status_code} - {resp.text}")
    except Exception as e:
        log(f"⚠️ שגיאת שליחה לטלגרם: {e}")


def format_alert(analysis: dict, original_snippet: str) -> str:
    """מעצב את ההתראה לטלגרם"""
    confidence = analysis.get("confidence", 0)
    project_type = analysis.get("project_type", "לא ידוע")
    summary = analysis.get("summary", "")
    reason = analysis.get("reason", "")
    suggested = analysis.get("suggested_reply", "")

    # אימוג'י לפי רמת confidence
    if confidence >= 90:
        emoji = "🔥"
    elif confidence >= 80:
        emoji = "🎯"
    else:
        emoji = "💡"

    msg = f"""{emoji} <b>הזדמנות חדשה!</b>

📊 <b>ביטחון:</b> {confidence}%
🏷 <b>סוג:</b> {project_type}

📌 <b>סיכום:</b>
{summary}

💡 <b>למה רלוונטי:</b>
{reason}"""

    if suggested:
        msg += f"""

✍️ <b>תגובה מוצעת:</b>
<i>{suggested}</i>"""

    # קטע מהפוסט המקורי
    snippet = original_snippet[:300].replace("<", "&lt;").replace(">", "&gt;")
    msg += f"""

📜 <b>מתוך הפוסט:</b>
<code>{snippet}...</code>"""

    return msg


# ──────────────────────────────────────────────
# לולאה ראשית
# ──────────────────────────────────────────────

def validate_config():
    """בדיקה שכל ההגדרות קיימות"""
    missing = []
    if not EMAIL_USER:
        missing.append("EMAIL_USER")
    # IMAP auth prerequisites
    ms_oauth_supported = IMAP_PROVIDER_RESOLVED in ("outlook", "office365", "microsoft")

    if IMAP_AUTH_METHOD in ("xoauth2", "oauth2") and not ms_oauth_supported:
        log("❌ IMAP_AUTH_METHOD=xoauth2 נתמך כאן רק עבור Outlook/Office365 (Microsoft OAuth).")
        log("ℹ️ עבור Gmail: הגדר IMAP_SERVER=imap.gmail.com, IMAP_AUTH_METHOD=basic והשתמש ב-EMAIL_PASS כסיסמת אפליקציה (App Password).")
        return False

    want_oauth = (IMAP_AUTH_METHOD in ("xoauth2", "oauth2")) or (
        IMAP_AUTH_METHOD == "auto" and ms_oauth_supported and MS_CLIENT_ID and MS_REFRESH_TOKEN
    )

    if want_oauth:
        if not MS_CLIENT_ID:
            missing.append("MS_CLIENT_ID")
        if not MS_REFRESH_TOKEN:
            missing.append("MS_REFRESH_TOKEN")
        # MS_CLIENT_SECRET תלוי בסוג האפליקציה, לכן לא מחייבים.
    else:
        if not EMAIL_PASS:
            missing.append("EMAIL_PASS")
    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        log(f"❌ חסרים משתני סביבה: {', '.join(missing)}")
        log("הגדר אותם ב-Render או ב-.env לפני הרצה")
        return False
    return True


def process_single_email(mail_conn, email_data: dict) -> bool:
    """מעבד מייל בודד. מחזיר True אם נשלחה התראה"""
    uid = email_data["uid"]
    subject = email_data["subject"]
    full_text = email_data["full_text"]

    # בדיקת כפילויות
    fp = make_fingerprint(subject, full_text)
    if is_duplicate(fp):
        log(f"⏭️ דילוג - כפילות: {subject[:50]}")
        mark_seen(mail_conn, uid)
        return False

    # סינון מהיר לפני AI
    filter_result = quick_keyword_filter(full_text)
    if filter_result != "pass":
        log(f"⏭️ דילוג - {filter_result}: {subject[:50]}")
        mark_seen(mail_conn, uid)
        return False

    # ניתוח AI
    log(f"🤖 מנתח עם AI: {subject[:50]}")
    analysis = analyze_with_ai(full_text)

    if analysis is None:
        handle_processing_failure(mail_conn, uid, subject, "AI לא החזיר JSON תקין/נכשל")
        return False

    is_relevant = analysis.get("is_relevant", False)
    confidence = int(analysis.get("confidence", 0))

    if is_relevant and confidence >= CONFIDENCE_THRESHOLD:
        alert_msg = format_alert(analysis, email_data["body"])
        send_telegram(alert_msg)
        log(f"✅ התראה נשלחה! (confidence: {confidence}%)")
        mark_seen(mail_conn, uid)
        return True
    else:
        reason = analysis.get("reason", "")
        log(f"⏭️ לא רלוונטי (confidence: {confidence}%): {reason[:80]}")
        mark_seen(mail_conn, uid)
        return False


def main_loop():
    """הלולאה הראשית - רצה כל CHECK_INTERVAL שניות"""
    if not validate_config():
        return

    log("🚀 Facebook Leads Bot התחיל לרוץ!")
    log(f"📧 מייל: {EMAIL_USER}")
    log(f"📨 IMAP: {IMAP_SERVER}:{IMAP_PORT} (provider={IMAP_PROVIDER_RESOLVED}, IMAP_AUTH_METHOD={IMAP_AUTH_METHOD})")
    log(f"⏱️ בדיקה כל {CHECK_INTERVAL} שניות")
    log(f"🎚️ סף confidence: {CONFIDENCE_THRESHOLD}%")
    log(f"🤖 מודל: {AI_MODEL}")

    send_telegram("🤖 <b>Facebook Leads Bot התחיל לרוץ!</b>\n\nמחפש הזדמנויות...")

    consecutive_errors = 0

    while True:
        try:
            log("🔎 בודק מיילים חדשים מפייסבוק...")
            mail_conn, emails = fetch_facebook_emails()

            if emails:
                log(f"📬 נמצאו {len(emails)} מיילים חדשים")
            else:
                log("📭 אין מיילים חדשים (UNSEEN) כרגע")

            alerts_sent = 0
            for email_data in emails:
                try:
                    if process_single_email(mail_conn, email_data):
                        alerts_sent += 1
                except Exception as e:
                    # לא משאירים את המייל כ-UNSEEN כדי לא להיתקע על אותו מייל שוב ושוב
                    try:
                        uid = email_data.get("uid")
                        if uid:
                            handle_processing_failure(
                                mail_conn,
                                uid,
                                email_data.get("subject", ""),
                                f"Exception: {e}",
                            )
                    except Exception:
                        pass
                    continue

            if emails:
                log(f"📊 סיכום: {len(emails)} מיילים, {alerts_sent} התראות")

            try:
                mail_conn.close()
                mail_conn.logout()
            except Exception:
                pass

            consecutive_errors = 0

        except imaplib.IMAP4.error as e:
            consecutive_errors += 1
            err_txt = str(e)
            log(f"⚠️ שגיאת IMAP ({consecutive_errors}): {err_txt}")
            if "BasicAuthBlocked" in err_txt or "BasicAuth" in err_txt:
                if IMAP_PROVIDER_RESOLVED == "outlook":
                    log("ℹ️ נראה ש-Outlook חוסם IMAP עם סיסמה (Basic Auth).")
                    log("ℹ️ פתרון: הגדר IMAP_AUTH_METHOD=xoauth2 והוסף MS_CLIENT_ID + MS_REFRESH_TOKEN (ואופציונלי MS_CLIENT_SECRET) ב-Render Secrets.")
                    log("ℹ️ אם התכוונת ל-Gmail: הגדר IMAP_SERVER=imap.gmail.com ו-IMAP_AUTH_METHOD=basic עם App Password.")
                else:
                    log("ℹ️ השרת מחזיר BasicAuthBlocked. בדוק שאתה מתחבר לשרת הנכון (ל-Gmail: IMAP_SERVER=imap.gmail.com, IMAP_AUTH_METHOD=basic עם App Password).")
            if consecutive_errors >= 5:
                log("❌ יותר מדי שגיאות IMAP ברצף - ממתין 10 דקות")
                time.sleep(600)
                consecutive_errors = 0

        except Exception as e:
            consecutive_errors += 1
            log(f"❌ שגיאה כללית ({consecutive_errors}): {e}")
            if consecutive_errors >= 10:
                log("❌ יותר מדי שגיאות - ממתין 10 דקות")
                time.sleep(600)
                consecutive_errors = 0

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main_loop()
