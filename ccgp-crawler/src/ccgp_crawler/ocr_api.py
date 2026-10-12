from fastapi import HTTPException
from .ocr_config import OCRConfigStore, OCRConfigUpdate, OCRConfigView
from .ocr_client import PaddleOCRClient, OCRTestResult


def register_ocr_routes(app,store:OCRConfigStore):
    app.state.ocr_config_store=store
    app.state.ocr_client=PaddleOCRClient()

    @app.get('/api/ocr/config',response_model=OCRConfigView)
    def get_config():return store.view()

    @app.put('/api/ocr/config',response_model=OCRConfigView)
    def save_config(payload:OCRConfigUpdate):return store.save(payload)

    @app.post('/api/ocr/test',response_model=OCRTestResult)
    async def test_config(payload:OCRConfigUpdate):
        config=store.resolve(payload)
        return await app.state.ocr_client.test(config)
