from fastapi import APIRouter

from app.api.routes.momentum import router as momentum_router

router = APIRouter(prefix='/api/v1')
router.include_router(momentum_router)


@router.get('/modules', tags=['模块'])
def modules():
    return {'modules': [{
        'id': 'industry-momentum', 'name': '行业动量',
        'description': '从行业热力分布到长期动量趋势，捕捉市场的集体方向。',
        'path': '/industry-momentum', 'status': 'available',
    }]}


@router.get('/health', tags=['状态'])
def health():
    return {'status': 'ok', 'service': 'showallopts-api'}
