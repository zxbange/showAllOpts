from fastapi import APIRouter

from app.api.routes.crowding import router as crowding_router
from app.api.routes.microcap import router as microcap_router
from app.api.routes.momentum import router as momentum_router

router = APIRouter(prefix='/api/v1')
router.include_router(momentum_router)
router.include_router(crowding_router)
router.include_router(microcap_router)


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
    }]}


@router.get('/health', tags=['状态'])
def health():
    return {'status': 'ok', 'service': 'showallopts-api'}
