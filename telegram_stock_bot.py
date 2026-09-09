import os
import asyncio
import logging
from datetime import datetime, timezone
import yfinance as yf
from telegram import Bot
from telegram.error import TelegramError
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", "3600"))  # seconds between full rounds

# minutes | hour | day | week
TIMEFRAME = os.getenv("TIMEFRAME", "hour").lower()

# Display name -> Yahoo Finance ticker.
# ⚠️ VERIFY EACH ONE with verify_symbols.py before trusting the bot's output.
# XAUUSD uses gold futures (GC=F) as the closest free proxy for spot gold.
# XAUAUD is NOT a standard Yahoo ticker — included as a guess, likely to fail.
SYMBOLS = {
    "EURUSD": "EURUSD=X",
    "AUDUSD": "AUDUSD=X",
    "USDJPY": "USDJPY=X",
    "BTC":    "BTC-USD",
    "XAUUSD":    "GC=F",  
}

# yfinance interval/period pairs per requested timeframe.
TIMEFRAME_CONFIG = {
    "minutes": {"interval": "15m", "period": "5d"},
    "hour":    {"interval": "1h",  "period": "1mo"},
    "day":     {"interval": "1d",  "period": "6mo"},
    "week":    {"interval": "1wk", "period": "2y"},
}

if TIMEFRAME not in TIMEFRAME_CONFIG:
    logger.warning(f"Unknown TIMEFRAME '{TIMEFRAME}', defaulting to 'hour'")
    TIMEFRAME = "hour"


# ── Analysis ──────────────────────────────────────────────────────────────
class SymbolAnalyzer:
    """Fetches price data for one symbol and produces a status + signal."""

    MIN_ROWS = 30  # minimum bars needed before indicators are trustworthy

    def __init__(self, display_name, ticker, timeframe):
        self.display_name = display_name
        self.ticker = ticker
        self.timeframe = timeframe
        self.data = None

    def fetch(self):
        cfg = TIMEFRAME_CONFIG[self.timeframe]
        t = yf.Ticker(self.ticker)
        data = t.history(period=cfg["period"], interval=cfg["interval"])
        if data is None or data.empty:
            raise ValueError(f"No data returned for {self.ticker} "
                              f"(interval={cfg['interval']}, period={cfg['period']})")
        if len(data) < self.MIN_ROWS:
            raise ValueError(f"Only {len(data)} bars returned for {self.ticker}, "
                              f"need at least {self.MIN_ROWS} for reliable signals")
        self.data = data
        return data

    def _rsi(self, window=14):
        delta = self.data["Close"].diff()
        gain = delta.where(delta > 0, 0).rolling(window).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window).mean()
        rs = gain / loss
        return (100 - (100 / (1 + rs))).iloc[-1]

    def _sma(self, window):
        w = min(window, len(self.data) - 1)
        return self.data["Close"].rolling(w).mean().iloc[-1]

    def _macd(self):
        ema12 = self.data["Close"].ewm(span=12, adjust=False).mean()
        ema26 = self.data["Close"].ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        return macd.iloc[-1], signal.iloc[-1]

    def analyze(self):
        close = self.data["Close"]
        current = close.iloc[-1]
        prev = close.iloc[-2]
        change_pct = ((current - prev) / prev) * 100

        rsi = self._rsi()
        sma20 = self._sma(20)
        sma50 = self._sma(50)
        macd, macd_signal = self._macd()

        buy_pts, sell_pts = 0, 0
        if rsi < 30:
            buy_pts += 2
        elif rsi > 70:
            sell_pts += 2

        if current > sma20 > sma50:
            buy_pts += 2
        elif current < sma20 < sma50:
            sell_pts += 2

        if macd > macd_signal:
            buy_pts += 1
        else:
            sell_pts += 1

        if buy_pts >= 5:
            rec, strength = "🟢 BUY", "STRONG"
        elif buy_pts > sell_pts:
            rec, strength = "🟢 BUY", "MODERATE"
        elif sell_pts >= 5:
            rec, strength = "🔴 SELL", "STRONG"
        elif sell_pts > buy_pts:
            rec, strength = "🔴 SELL", "MODERATE"
        else:
            rec, strength = "🟡 HOLD", "NEUTRAL"

        if current > sma20 > sma50:
            status = "Uptrend"
        elif current < sma20 < sma50:
            status = "Downtrend"
        else:
            status = "Sideways / mixed"

        return {
            "display_name": self.display_name,
            "ticker": self.ticker,
            "timeframe": self.timeframe,
            "price": round(current, 5) if current < 10 else round(current, 2),
            "change_pct": round(change_pct, 2),
            "rsi": round(rsi, 1),
            "status": status,
            "recommendation": rec,
            "strength": strength,
        }

    def report(self):
        self.fetch()
        return self.analyze()


# ── Telegram ──────────────────────────────────────────────────────────────
class MarketBot:
    def __init__(self, token, chat_id, symbols, timeframe):
        self.bot = Bot(token=token)
        self.chat_id = chat_id
        self.symbols = symbols
        self.timeframe = timeframe
        self.last_message_ids = {}  # chat_id -> message_id

    def _format_line(self, r):
        arrow = "📈" if r["change_pct"] >= 0 else "📉"
        return (f"<b>{r['display_name']}</b>  {arrow} {r['price']} "
                f"({r['change_pct']:+.2f}%)\n"
                f"Status: {r['status']} | RSI: {r['rsi']}\n"
                f"{r['recommendation']} — {r['strength']}")

    async def send_update(self, timeframe=None, chat_id=None):
        timeframe = timeframe or self.timeframe
        chat_id = chat_id or self.chat_id
        lines = []
        failures = []

        for name, ticker in self.symbols.items():
            try:
                analyzer = SymbolAnalyzer(name, ticker, timeframe)
                report = analyzer.report()
                lines.append(self._format_line(report))
            except Exception as e:
                logger.error(f"Skipping {name} ({ticker}): {e}")
                failures.append(name)

        if not lines:
            logger.error("No symbols returned data this round — message not sent")
            return

        header = f"<b>📊 Market Update — {timeframe.upper()}</b>\n"
        header += f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        header += "━━━━━━━━━━━━━━━━━\n"

        body = "\n\n".join(lines)

        footer = "\n\n<i>⚠️ Not financial advice. Verify against your data source before trading.</i>"
        if failures:
            footer += f"\n<i>No data for: {', '.join(failures)}</i>"

        message = header + body + footer

        try:
            old_id = self.last_message_ids.get(chat_id)
            if old_id:
                try:
                    await self.bot.delete_message(chat_id=chat_id, message_id=old_id)
                except TelegramError:
                    pass  # message may already be gone/too old to delete

            sent = await self.bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
            self.last_message_ids[chat_id] = sent.message_id
            logger.info(f"Update sent ({len(lines)}/{len(self.symbols)} symbols)")
        except TelegramError as e:
            logger.error(f"Failed to send Telegram message: {e}")
async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("⏱ Minutes", callback_data="minutes")],
        [InlineKeyboardButton("🕐 Hour", callback_data="hour")],
        [InlineKeyboardButton("📅 Day", callback_data="day")],
        [InlineKeyboardButton("🗓 Week", callback_data="week")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Choose an update timeframe:", reply_markup=reply_markup)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chosen_timeframe = query.data
    requsting_chat_id = query.message.chat_id

    bot_instance = context.bot_data["market_bot"]
    await query.edit_message_text(f"Fetching {chosen_timeframe} update...")
    await bot_instance.send_update(timeframe=chosen_timeframe, chat_id=requsting_chat_id)


async def post_init(application):
    application.create_task(background_loop(application))


async def background_loop(application):
    market_bot = application.bot_data["market_bot"]
    while True:
        await market_bot.send_update()
        await asyncio.sleep(UPDATE_INTERVAL)


def main():
    if not TELEGRAM_BOT_TOKEN or not CHAT_ID:
        logger.error("Missing TELEGRAM_BOT_TOKEN or CHAT_ID in .env")
        return

    market_bot = MarketBot(TELEGRAM_BOT_TOKEN, CHAT_ID, SYMBOLS, TIMEFRAME)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.bot_data["market_bot"] = market_bot
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info(f"Bot started. Timeframe={TIMEFRAME}, "
                f"symbols={list(SYMBOLS.keys())}, "
                f"interval={UPDATE_INTERVAL}s")

    app.run_polling()


if __name__ == "__main__":
    main()