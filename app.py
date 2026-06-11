from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import traceback

app = Flask(__name__)
CORS(app)


def safe_val(val):
    if val is None:
        return None
    try:
        if hasattr(val, 'item'):
            val = val.item()
        f = float(val)
        if f != f:  # NaN check
            return None
        return f
    except (TypeError, ValueError):
        return None


@app.route('/api/stock/<ticker>')
def get_stock(ticker):
    try:
        t = yf.Ticker(ticker.upper())
        info = t.info

        # 현재가
        price = safe_val(info.get('currentPrice') or info.get('regularMarketPrice'))

        # 52주 범위
        low52 = safe_val(info.get('fiftyTwoWeekLow'))
        high52 = safe_val(info.get('fiftyTwoWeekHigh'))

        # 최근 1년 종가 히스토리 (차트용 - 월별)
        hist = t.history(period='1y', interval='1mo')
        history = []
        if not hist.empty:
            for dt, row in hist.iterrows():
                history.append({
                    'date': dt.strftime('%Y-%m'),
                    'close': round(float(row['Close']), 2)
                })

        data = {
            'ticker': ticker.upper(),
            'name': info.get('longName') or info.get('shortName') or ticker.upper(),
            'sector': info.get('sector', '-'),
            'industry': info.get('industry', '-'),
            'currency': info.get('currency', 'USD'),
            'exchange': info.get('exchange', '-'),

            # 가격
            'price': price,
            'change': safe_val(info.get('regularMarketChange')),
            'changePercent': safe_val(info.get('regularMarketChangePercent')),
            'open': safe_val(info.get('regularMarketOpen')),
            'prevClose': safe_val(info.get('regularMarketPreviousClose')),
            'dayLow': safe_val(info.get('regularMarketDayLow')),
            'dayHigh': safe_val(info.get('regularMarketDayHigh')),
            'low52': low52,
            'high52': high52,
            'volume': safe_val(info.get('regularMarketVolume')),
            'avgVolume': safe_val(info.get('averageVolume')),

            # 밸류에이션
            'per': safe_val(info.get('trailingPE')),
            'forwardPer': safe_val(info.get('forwardPE')),
            'pbr': safe_val(info.get('priceToBook')),
            'psr': safe_val(info.get('priceToSalesTrailing12Months')),
            'evEbitda': safe_val(info.get('enterpriseToEbitda')),
            'evRevenue': safe_val(info.get('enterpriseToRevenue')),

            # 수익성
            'roe': safe_val(info.get('returnOnEquity')),
            'roa': safe_val(info.get('returnOnAssets')),
            'grossMargin': safe_val(info.get('grossMargins')),
            'operatingMargin': safe_val(info.get('operatingMargins')),
            'netMargin': safe_val(info.get('profitMargins')),
            'ebitdaMargin': safe_val(info.get('ebitdaMargins')),

            # 성장성
            'revenueGrowth': safe_val(info.get('revenueGrowth')),
            'earningsGrowth': safe_val(info.get('earningsGrowth')),
            'earningsQuarterlyGrowth': safe_val(info.get('earningsQuarterlyGrowth')),

            # 재무 건전성
            'debtToEquity': safe_val(info.get('debtToEquity')),
            'currentRatio': safe_val(info.get('currentRatio')),
            'quickRatio': safe_val(info.get('quickRatio')),
            'totalCash': safe_val(info.get('totalCash')),
            'totalDebt': safe_val(info.get('totalDebt')),
            'freeCashflow': safe_val(info.get('freeCashflow')),

            # 규모
            'marketCap': safe_val(info.get('marketCap')),
            'enterpriseValue': safe_val(info.get('enterpriseValue')),
            'revenue': safe_val(info.get('totalRevenue')),
            'ebitda': safe_val(info.get('ebitda')),
            'eps': safe_val(info.get('trailingEps')),
            'forwardEps': safe_val(info.get('forwardEps')),
            'bookValue': safe_val(info.get('bookValue')),

            # 배당
            'dividendYield': safe_val(info.get('dividendYield')),
            'dividendRate': safe_val(info.get('dividendRate')),
            'payoutRatio': safe_val(info.get('payoutRatio')),
            'exDividendDate': info.get('exDividendDate'),

            # 애널리스트
            'targetPrice': safe_val(info.get('targetMeanPrice')),
            'targetLow': safe_val(info.get('targetLowPrice')),
            'targetHigh': safe_val(info.get('targetHighPrice')),
            'recommendationMean': safe_val(info.get('recommendationMean')),
            'recommendation': info.get('recommendationKey', '-'),
            'numberOfAnalysts': info.get('numberOfAnalystOpinions'),

            # 기타
            'beta': safe_val(info.get('beta')),
            'sharesOutstanding': safe_val(info.get('sharesOutstanding')),
            'floatShares': safe_val(info.get('floatShares')),
            'shortRatio': safe_val(info.get('shortRatio')),

            'history': history,
        }
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/batch')
def get_batch():
    tickers = request.args.get('tickers', '')
    ticker_list = [t.strip() for t in tickers.split(',') if t.strip()]
    results = {}
    for ticker in ticker_list:
        try:
            resp = get_stock(ticker)
            results[ticker.upper()] = resp.get_json()
        except Exception as e:
            results[ticker.upper()] = {'success': False, 'error': str(e)}
    return jsonify(results)


if __name__ == '__main__':
    app.run(debug=True, port=5001)
