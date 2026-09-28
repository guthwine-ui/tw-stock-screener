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

# 從 GitHub Secrets 讀取帳密
SENDER_EMAIL = os.environ.get("SENDER_EMAIL")
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL")

# FinMind API Token（若有可在 Secrets 新增 FINMIND_TOKEN，留空則以公開頻率抓取）
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")


def get_all_twse_stocks() -> list[dict]:
    """從證交所 OpenAPI 自動抓取目前所有本國上市普通股"""
    url = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
    try:
        res = requests.get(url, timeout=15)
        data = res.json()
        stocks = []
        for item in data:
            code = item.get("Code", "")
            name = item.get("Name", "")
            # 篩選 4 位數純數字的普通股（排除權證、特別股等）
            if len(code) == 4 and code.isdigit():
                stocks.append({"code": code, "name": name})
        print(f"成功取得上市普通股清單，共 {len(stocks)} 檔。")
        return stocks
    except Exception as e:
        print(f"取得全市場股票清單失敗: {e}，改用預設權值股池。")
        return [
            {"code": "2330", "name": "台積電"},
            {"code": "2317", "name": "鴻海"},
            {"code": "2454", "name": "聯發科"},
            {"code": "2382", "name": "廣達"},
            {"code": "2308", "name": "台達電"},
            {"code": "2603", "name": "長榮"},
            {"code": "2881", "name": "富邦金"},
            {"code": "2882", "name": "國泰金"},
            {"code": "3231", "name": "緯創"},
            {"code": "2379", "name": "瑞昱"},
        ]


def check_price_above_ma60(stock_id: str) -> tuple[bool, float, float]:
    """第一關：檢查最新股價是否站上 60MA 季線"""
    try:
        ticker = yf.Ticker(f"{stock_id}.TW")
        df = ticker.history(period="6mo")
        if df.empty or len(df) < 60:
            return False, 0.0, 0.0

        df["MA60"] = df["Close"].rolling(window=60).mean()
        latest_close = df["Close"].iloc[-1]
        latest_ma60 = df["MA60"].iloc[-1]

        is_above = latest_close > latest_ma60
        return is_above, round(latest_close, 2), round(latest_ma60, 2)
    except Exception:
        return False, 0.0, 0.0


def check_foreign_buy(stock_id: str, days: int = 3) -> bool:
    """第二關：檢查外資是否連續 3 個交易日維持買超"""
    url = "https://api.finmindtrade.com/api/v4/data"
    start_date = (
        datetime.datetime.now() - datetime.timedelta(days=20)
    ).strftime("%Y-%m-%d")
    params = {
        "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
        "data_id": stock_id,
        "start_date": start_date,
    }
    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN

    try:
        res = requests.get(url, params=params, timeout=10)
        data = res.json().get("data", [])
        if not data:
            return False

        df = pd.DataFrame(data)
        df_foreign = df[df["name"] == "Foreign_Investor"].copy()
        if df_foreign.empty or len(df_foreign) < days:
            return False

        df_foreign["date"] = pd.to_datetime(df_foreign["date"])
        df_foreign = df_foreign.sort_values("date").tail(days)
        df_foreign["net_buy"] = df_foreign["buy"] - df_foreign["sell"]

        return (df_foreign["net_buy"] > 0).all()
    except Exception:
        return False


def get_and_check_revenue_yoy(stock_id: str) -> tuple[bool, str]:
    """第三關：檢查近 3 個月營收 YoY 是否連續大於 0"""
    url = "https://api.finmindtrade.com/api/v4/data"
    start_date = (
        datetime.datetime.now() - datetime.timedelta(days=450)
    ).strftime("%Y-%m-%d")
    params = {
        "dataset": "TaiwanStockMonthRevenue",
        "data_id": stock_id,
        "start_date": start_date,
    }
    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN

    try:
        res = requests.get(url, params=params, timeout=10)
        data = res.json().get("data", [])
        if not data:
            return False, ""

        df = pd.DataFrame(data)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

        if "revenue" not in df.columns or len(df) < 15:
            return False, ""

        # 計算 YoY（當期 vs 前 12 個月）
        df["revenue_yoy"] = df["revenue"].pct_change(periods=12) * 100
        recent_3m = df["revenue_yoy"].dropna().tail(3)

        if len(recent_3m) < 3 or not (recent_3m > 0).all():
            return False, ""

        latest_yoy = f"{round(recent_3m.iloc[-1], 2)}%"
        return True, latest_yoy
    except Exception:
        return False, ""


def send_email_report(matched_stocks: list[dict]):
    """產生精美 HTML 表格並寄信"""
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(
        f"【台股全市場強勢股篩選】{today_str} 營收連三揚+站上季線+外資連三買",
        "utf-8",
    )
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECIPIENT_EMAIL

    if not matched_stocks:
        html_body = f"""
        <html><body>
            <h3>📊 台股全市場篩選日報（{today_str}）</h3>
            <p>今日全市場上市股票中，無符合「營收連3揚 ＋ 股價在季線之上 ＋ 外資連3買」之標的。</p>
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
                th, td {{ padding: 10px 14px; text-align: center; border: 1px solid #ddd; }}
                th {{ background-color: #2c3e50; color: white; }}
                tr:nth-child(even) {{ background-color: #f8f9fa; }}
            </style>
        </head>
        <body>
            <h3>📊 台股全市場篩選結果（{today_str}）</h3>
            <p><strong>選股條件：</strong></p>
            <ul>
                <li>股價大於 60MA 季線</li>
                <li>外資連續 3 個交易日買超</li>
                <li>近 3 個月營收年增率 (YoY) 皆維持正成長</li>
            </ul>
            <br>
            {table_html}
            <br>
            <p style="color: gray; font-size: 12px;">* 篩選標的為全體上市普通股，投資請審慎評估風險。</p>
        </body>
        </html>
        """

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, [RECIPIENT_EMAIL], msg.as_string())
    print("選股報告郵件已成功寄出！")


def main():
    stock_candidates = get_all_twse_stocks()
    matched = []

    print(f"開始掃描全市場（共 {len(stock_candidates)} 檔）...")

    for idx, item in enumerate(stock_candidates, start=1):
        sid = item["code"]
        name = item["name"]

        # 第一關：股價是否站上季線
        is_above, close_val, ma_val = check_price_above_ma60(sid)
        if not is_above:
            continue

        # 第二關：外資是否連續 3 天買超
        if not check_foreign_buy(sid, days=3):
            continue

        # 第三關：近 3 個月營收年增率是否全部正成長
        is_rev_pass, latest_yoy = get_and_check_revenue_yoy(sid)
        if not is_rev_pass:
            continue

        print(f"🎯 符合條件：{sid} {name} (收盤: {close_val}, 最新YoY: {latest_yoy})")
        matched.append(
            {
                "股票代號": sid,
                "股票名稱": name,
                "最新收盤價": close_val,
                "60MA 季線": ma_val,
                "最新營收 YoY": latest_yoy,
                "外資動向": "連買 3 日以上",
            }
        )

        time.sleep(0.3)

    print(f"篩選完成，共找到 {len(matched)} 檔符合條件個股。")
    send_email_report(matched)


if __name__ == "__main__":
    main()
