import datetime
import os
import smtplib
import time
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import pandas as pd
import requests
import yfinance as yf

# 從 GitHub Secrets 讀取敏感帳密資訊
SENDER_EMAIL = os.environ.get("SENDER_EMAIL")
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL")

# 欲掃描的標的清單（可自行擴增股票代號）
STOCK_LIST = [
    "2330",
    "2317",
    "2454",
    "2382",
    "2308",
    "2603",
    "2881",
    "2882",
    "3231",
    "2379",
]


def get_monthly_revenue(stock_id: str) -> pd.DataFrame:
    """抓取近 400 天營收並計算年增率 (YoY)"""
    url = "https://api.finmindtrade.com/api/v4/data"
    start_date = (
        datetime.datetime.now() - datetime.timedelta(days=400)
    ).strftime("%Y-%m-%d")
    params = {
        "dataset": "TaiwanStockMonthRevenue",
        "data_id": stock_id,
        "start_date": start_date,
    }
    try:
        res = requests.get(url, params=params, timeout=10)
        data = res.json().get("data", [])
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        if "revenue" in df.columns and len(df) >= 13:
            df["revenue_yoy"] = df["revenue"].pct_change(periods=12) * 100
        else:
            df["revenue_yoy"] = None
        return df
    except Exception:
        return pd.DataFrame()


def check_revenue_condition(df_rev: pd.DataFrame) -> bool:
    """檢查最新連續 3 個月營收 YoY 是否都大於 0"""
    if df_rev.empty or len(df_rev) < 15:
        return False
    recent_3m = df_rev["revenue_yoy"].dropna().tail(3)
    if len(recent_3m) < 3:
        return False
    return (recent_3m > 0).all()


def check_price_above_ma60(stock_id: str):
    """檢查最新收盤價是否站上 60 日季線"""
    for suffix in [".TW", ".TWO"]:
        try:
            ticker = yf.Ticker(f"{stock_id}{suffix}")
            df = ticker.history(period="6mo")
            if not df.empty and len(df) >= 60:
                df["MA60"] = df["Close"].rolling(window=60).mean()
                latest_close = df["Close"].iloc[-1]
                latest_ma60 = df["MA60"].iloc[-1]
                return (
                    (latest_close > latest_ma60),
                    round(latest_close, 2),
                    round(latest_ma60, 2),
                )
        except Exception:
            continue
    return False, 0.0, 0.0


def check_foreign_buy(stock_id: str, days=3) -> bool:
    """檢查外資是否連續 N 天買超"""
    url = "https://api.finmindtrade.com/api/v4/data"
    start_date = (
        datetime.datetime.now() - datetime.timedelta(days=20)
    ).strftime("%Y-%m-%d")
    params = {
        "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
        "data_id": stock_id,
        "start_date": start_date,
    }
    try:
        res = requests.get(url, params=params, timeout=10)
        data = res.json().get("data", [])
        if not data:
            return False
        df = pd.DataFrame(data)
        df_foreign = df[df["name"] == "Foreign_Investor"].copy()
        if df_foreign.empty or len(df_foreign) < days:
            return False
        df_foreign = df_foreign.sort_values("date").tail(days)
        df_foreign["net_buy"] = df_foreign["buy"] - df_foreign["sell"]
        return (df_foreign["net_buy"] > 0).all()
    except Exception:
        return False


def send_email_report(matched_stocks):
    """寄送 HTML 報表"""
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(
        f"【台股自動選股日報】{today_str} 營收連三揚+季線之上+外資連三買",
        "utf-8",
    )
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECIPIENT_EMAIL

    if not matched_stocks:
        html_body = f"""
        <html><body>
            <h3>📊 台股篩選日報（{today_str}）</h3>
            <p>今日追蹤清單中，無符合全部條件的個股。</p>
        </body></html>
        """
    else:
        df_res = pd.DataFrame(matched_stocks)
        table_html = df_res.to_html(index=False, border=1, justify="center")
        html_body = f"""
        <html>
        <head>
            <style>
                table {{ border-collapse: collapse; width: 100%; font-family: sans-serif; }}
                th, td {{ padding: 8px 12px; text-align: center; border: 1px solid #ddd; }}
                th {{ background-color: #1a73e8; color: white; }}
                tr:nth-child(even) {{ background-color: #f8f9fa; }}
            </style>
        </head>
        <body>
            <h3>📊 台股篩選結果（{today_str}）</h3>
            <p>條件：近 3 個月營收年增率皆正成長 ＋ 股價在 60MA 季線之上 ＋ 外資連續 3 天買超</p>
            {table_html}
        </body>
        </html>
        """

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, [RECIPIENT_EMAIL], msg.as_string())
    print("郵件已成功寄出！")


def main():
    matched = []
    print("開始執行篩選...")
    for sid in STOCK_LIST:
        # 1. 季線
        is_above, close_val, ma_val = check_price_above_ma60(sid)
        if not is_above:
            continue

        # 2. 營收連三月正成長
        df_rev = get_monthly_revenue(sid)
        if not check_revenue_condition(df_rev):
            continue

        # 3. 外資連三買
        if not check_foreign_buy(sid, days=3):
            continue

        latest_yoy = round(df_rev["revenue_yoy"].dropna().iloc[-1], 2)
        matched.append(
            {
                "股票代號": sid,
                "收盤價": close_val,
                "季線(60MA)": ma_val,
                "最新營收YoY": f"{latest_yoy}%",
                "外資動向": "連買 3 日以上",
            }
        )
        time.sleep(0.3)

    print(f"篩選完成，共 {len(matched)} 檔。")
    send_email_report(matched)


if __name__ == "__main__":
    main()
