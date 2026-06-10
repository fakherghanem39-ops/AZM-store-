"""
main.py — AZM Store
يُشغّل خادم Flask (API + Mini App) وبوت تيليغرام في نفس العملية.

التشغيل:
    python3 main.py

متغيرات البيئة (اختيارية):
    HOST_DOMAIN=alustura-store.com       ← دومينك (يُضاف /shop/ تلقائياً)
    WEBAPP_URL=https://alustura-store.com     ← رابط كامل بديل
    PORT=8081                      ← منفذ خادم Flask (افتراضي 8081)
"""
import threading
import os
import sys
import time

# ── تأكد أن المجلد الحالي هو مجلد main.py ────────────────────────────────────
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ── تشغيل خادم Flask في خيط خلفي ────────────────────────────────────────────
def _start_flask_server():
    try:
        from server import app, PORT, WEBAPP_BUILD
        print(f"🌐 Flask server → http://0.0.0.0:{PORT}/api/shop/")
        if os.path.isdir(WEBAPP_BUILD):
            print(f"📱 Mini App    → http://0.0.0.0:{PORT}/shop/")
        else:
            print(f"⚠️  Mini App build not found at: {WEBAPP_BUILD}")
        app.run(
            host="0.0.0.0",
            port=PORT,
            debug=False,
            threaded=True,
            use_reloader=False,
        )
    except Exception as e:
        print(f"❌ Flask server error: {e}")
        import traceback
        traceback.print_exc()


flask_thread = threading.Thread(target=_start_flask_server, daemon=True, name="flask-server")
flask_thread.start()

# أعط Flask ثانية واحدة للبدء قبل تشغيل البوت
time.sleep(1)

# ── تشغيل البوت في الخيط الرئيسي ─────────────────────────────────────────────
try:
    import bot
    bot.main()
except KeyboardInterrupt:
    print("\n🛑 تم إيقاف البوت.")
    sys.exit(0)
