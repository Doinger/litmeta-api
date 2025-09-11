# server.py
from fastapi import FastAPI, Request
from datetime import datetime

app = FastAPI(title="Academic Analyzer Mock", version="1.0")

# 占位：返回一个与 AcademicBatchV1_1 结构一致的最小合法 JSON
def make_placeholder_batch():
    return {
        "batch_id": datetime.utcnow().strftime("%Y%m%d-%H%M%S"),
        "papers": [
            {
                "paper_id": "P1",
                "filename": "",
                "meta": {
                    "title": "",
                    "year": "",
                    "journal": "",
                    "doi": "",
                    "pmid": ""
                },
                "sections": [
                    {
                        "section_title": "Introduction",
                        "paragraphs": [
                            {
                                "para_index": 1,
                                "text_preview": "占位预览（这里应≤200字；真实解析由模型生成）。",
                                "key_points": [
                                    "提出研究空白",
                                    "界定目标人群"
                                ],
                                "evidence": [],
                                "claims_strength": "medium",
                                "limitations_flags": []
                            }
                        ]
                    }
                ],
                "segmentation_audit": {
                    "sections_detected": 1,
                    "paragraphs_detected": 1,
                    "paragraphs_reported": 1
                }
            }
        ]
    }

@app.post("/analyze")
async def analyze(request: Request):
    # 如果前端传了参数，这里收一下但不强制使用
    try:
        body = await request.json()
    except Exception:
        body = {}
    # 为了让 Actions 的“测试响应”也通过，我们直接返回占位结构
    return make_placeholder_batch()
