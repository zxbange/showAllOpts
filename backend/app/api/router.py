from fastapi import APIRouter
from app.api.routes.growth import router as growth_router
from app.api.routes.cache import router as cache_router
from app.api.routes.dividend import router as dividend_router

from app.api.routes.breadth import router as breadth_router
from app.api.routes.earnings import router as earnings_router
from app.api.routes.etf import router as etf_router
from app.api.routes.crowding import router as crowding_router
from app.api.routes.microcap import router as microcap_router
from app.api.routes.momentum import router as momentum_router

router = APIRouter(prefix='/api/v1')
router.include_router(momentum_router)
router.include_router(crowding_router)
router.include_router(microcap_router)
router.include_router(etf_router)
router.include_router(earnings_router)
router.include_router(breadth_router)
router.include_router(dividend_router)
router.include_router(cache_router)
router.include_router(growth_router)


@router.get('/modules', tags=['模块'])
def modules():
    return {'modules': [{
        'id': 'industry-momentum', 'name': '行业动量',
        'description': '从行业热力分布到长期动量趋势，捕捉市场的集体方向。',
        'path': '/industry-momentum', 'status': 'available',
    }, {
        'id': 'market-crowding', 'name': '抱团拥挤度',
        'description': '观察前 5% 股票成交额占比，追踪市场成交的集中与分散。',
        'path': '/market-crowding', 'status': 'available',
    }, {
        'id': 'microcap-temperature', 'name': '微盘温度计',
        'description': '从估值与交易拥挤度，观察微盘 1000 的温度与等权指数。',
        'path': '/microcap-temperature', 'status': 'available',
    }, {
        'id': 'earnings-gap', 'name': '净利润断层',
        'description': '筛选最新财报季度的业绩与价格共振，查看公告证据与估值数据。',
        'path': '/earnings-gap', 'status': 'available',
    }, {
        'id': 'etf-shares', 'name': 'ETF份额仪表盘',
        'description': '跟踪沪深300与中证500 ETF 总份额及份额变化率。',
        'path': '/etf-shares', 'status': 'available',
    }, {
        'id': 'microcap-breadth', 'name': '微盘新高新低',
        'description': '观察微盘400的新高新低数量、市场宽度与强度趋势。',
        'path': '/microcap-breadth', 'status': 'available',
    }, {
        'id': 'dividend-selection', 'name': '高股息选股',
        'description': '两种高股息规则，各取前 25，只读展示最新选股结果。',
        'path': '/dividend-selection', 'status': 'available',
    }, {
        'id': 'high-growth', 'name': '高增长',
        'description': '年报与三类季报分别检验归母净利润及 EPS 的持续增长。',
        'path': '/high-growth', 'status': 'available',
    }]}


@router.get('/health', tags=['状态'])
def health():
    return {'status': 'ok', 'service': 'showallopts-api'}
