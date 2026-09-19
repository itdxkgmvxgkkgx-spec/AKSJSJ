# ⚡ VLESS Forge — Generate & Test VLESS Configs

تولید، تست و رتبه‌بندی خودکار کانفیگ‌های VLESS از یک لینک اشتراک — با GitHub Actions، تست واقعی با Xray-core و یک UI زنده و مدرن.

## چی کار می‌کنه؟
1. لینک اشتراک (`BASE_URL`) را می‌گیرد (base64 یا متن خام) — با درخواست‌های موازی «hedged» تا کندی سرور منبع اجرا را معطل نکند.
2. تمام `vless://` ها را پارس می‌کند و برای هر هاست، IPهای اضافه را از DNS سیستم + Cloudflare/Google DoH پیدا می‌کند.
3. `TOTAL_GENERATE` واریانت می‌سازد: **fingerprint × آدرس (دامنه/IP) × spiderX** — به‌صورت round-robin تا خروجی متنوع بماند.
4. همه را **همزمان و async** تست می‌کند:
   - `tcp` → فقط اتصال TCP
   - `tls` → اتصال + TLS handshake با SNI درست (پیش‌فرض اگر Xray نبود)
   - `xray` → **تست واقعی**: هر کانفیگ داخل Xray-core بالا می‌آید و یک درخواست واقعی از آن رد می‌شود
   - `auto` → اگر باینری xray بود `xray`، وگرنه `tls`
5. خروجی‌ها را می‌نویسد و در ریپو کامیت می‌کند.

## خروجی‌ها (ریشه ریپو، بعد از هر اجرا)
| فایل | توضیح |
|---|---|
| `result.txt` | لینک‌های سالم، مرتب‌شده بر اساس پینگ، با نام `🇹🇷 01 ⚡216ms | chrome | ...` |
| `result_b64.txt` | همان لیست به‌صورت base64 → **لینک اشتراک** قابل import در v2rayNG / Hiddify / Streisand |
| `result.json` | جزئیات کامل هر کانفیگ (tcp_ms, tls_ms, xray_ms, خطا، SNI، fp …) + اطلاعات اشتراک |
| `all_generated.txt` | همهٔ کانفیگ‌های تولیدشده (سالم و ناسالم) |

لینک اشتراک همیشه‌به‌روز:
```
https://raw.githubusercontent.com/<OWNER>/<REPO>/<BRANCH>/result_b64.txt
```

## GitHub Actions
فایل: `.github/workflows/generate.yml`
- اجرای خودکار هر ۶ ساعت + اجرای دستی با ورودی‌ها: `total`, `threads`, `test_mode`, `ui_minutes`
- Xray-core و cloudflared به‌صورت خودکار نصب می‌شوند
- نتایج کامیت می‌شوند (`[skip ci]`) + به‌عنوان Artifact آپلود می‌شوند (۱۴ روز)
- **Job Summary** فارسی با جدول آمار، لینک اشتراک و ۱۰ کانفیگ برتر
- در اجرای دستی با `ui_minutes > 0`: UI با **Cloudflare Quick Tunnel** (بدون حساب) بالا می‌آید و آدرس `*.trycloudflare.com` در Summary و Annotation نمایش داده می‌شود؛ تا پایان زمان تعیین‌شده زنده می‌ماند و اگر از UI دوباره تولید کنید نتیجه به ریپو sync می‌شود.

## UI زنده (`ui_server.py` — فقط stdlib)
- داشبورد شیشه‌ای تیره/روشن، RTL فارسی، فونت Vazirmatn
- پیشرفت زنده با SSE (`/events`)، ETA، آمار (تولید/سالم/ناموفق/بهترین پینگ/مدت)
- نمایش اطلاعات اشتراک (مصرف، حجم کل، انقضا) از هدر `subscription-userinfo`
- جدول قابل مرتب‌سازی/جستجو/فیلتر با مدال 🥇🥈🥉، نوار پینگ رنگی، دکمه کپی و **QR** برای هر کانفیگ
- کپی همهٔ سالم‌ها، دانلود هر ۴ فایل، QR لینک اشتراک، لاگ زنده رنگی
- تنظیمات تولید (تعداد، نخ، حالت تست، تکرار، تایم‌اوت) از داخل مرورگر و شروع/توقف اجرا

### API
| مسیر | توضیح |
|---|---|
| `GET /` | UI |
| `GET /api/status` · `GET /events` | وضعیت زنده (JSON / SSE) |
| `GET /api/results` | محتوای `result.json` |
| `POST /api/start` `{total,threads,mode,rounds,timeout}` | شروع تولید (409 اگر در حال اجرا) |
| `GET /api/stop` | توقف |
| `GET /sub` · `GET /sub/b64` | اشتراک متنی / base64 |
| `GET /download/<file>` | دانلود `result.txt` `result_b64.txt` `result.json` `all_generated.txt` |

## اجرای محلی
```bash
pip install -r requirements.txt
# تولید و تست (خروجی در ./out)
OUT_DIR=out python generator.py --total 50 --threads 32 --mode auto
# UI
OUT_DIR=out python ui_server.py        # http://localhost:8000
# یا با PM2:
pm2 start ecosystem.config.cjs
```
برای تست واقعی، باینری `xray` را در PATH بگذارید (workflow خودش نصب می‌کند).

### پارامترها (env یا CLI)
`BASE_URL` · `OUTPUT_FILE` · `THREADS` · `TOTAL_GENERATE` · `TEST_MODE(auto|tls|tcp|xray)` · `TIMEOUT` · `ROUNDS` · `XRAY_BIN` · `OUT_DIR`

## نکات فنی
- سرور منبع گاهی ۲۰–۱۳۰ ثانیه در TCP connect گیر می‌کند؛ `_hedged_get` چند درخواست پله‌ای موازی می‌زند و اولین پاسخ را برمی‌دارد؛ اگر همه شکست بخورند از `source_cache.txt` استفاده می‌شود.
- تست ۵۰ کانفیگ با ۳۲ نخ در حالت TLS حدود ۳ ثانیه است؛ حالت Xray واقعی حدود ۱۰–۲۰ ثانیه.
- هیچ دیتابیسی لازم نیست؛ همه‌چیز فایل است و از طریق git منتشر می‌شود.

## ساختار
```
generator.py                 هستهٔ تولید/تست
ui_server.py                 وب‌سرور UI + API
static/{index.html,app.css,app.js}
.github/workflows/generate.yml
requirements.txt · ecosystem.config.cjs
```

**آخرین بروزرسانی:** 2026-09-19
