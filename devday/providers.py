"""Only this boundary sends data externally. Replay makes no API calls."""
import base64
import json
import os
from pathlib import Path
from typing import Protocol
from .contracts import ROIPlan, Diagnosis

class ModelProvider(Protocol):
    mode: str
    def propose_rois(self, metadata, images, correction=None) -> ROIPlan: ...
    def diagnose(self, evidence, images) -> Diagnosis: ...

class OpenAIProvider:
    mode='openai_live'
    def __init__(self, model=None, client=None):
        self.model=model or os.environ.get('DEVDAY_MODEL','gpt-6-luna')
        if client is None:
            if not os.environ.get('OPENAI_API_KEY'): raise RuntimeError('Set OPENAI_API_KEY; use replay/demo for offline verification')
            from openai import OpenAI
            client=OpenAI(timeout=180,max_retries=0)
        self.client=client
        self.calls=[]

    def _call(self, stage, payload, images, schema):
        content=[{'type':'input_text','text':json.dumps(payload,ensure_ascii=False,allow_nan=False)}]
        for label,path in images:
            p=Path(path)
            mime='image/png' if p.suffix.lower()=='.png' else 'image/jpeg'
            content.extend([{'type':'input_text','text':label}, {'type':'input_image','image_url':f'data:{mime};base64,'+base64.b64encode(p.read_bytes()).decode(),'detail':'high'}])
        prompt=(Path(__file__).parent/'prompts'/f'{stage}.txt').read_text(encoding='utf-8')
        response=self.client.responses.parse(model=self.model,instructions=prompt,input=[{'role':'user','content':content}],text_format=schema,store=False,max_output_tokens=7000)
        usage=getattr(response,'usage',None)
        self.calls.append({'stage':stage,'response_id':getattr(response,'id',None),'model':self.model,'usage':usage.model_dump() if usage else None})
        if getattr(response,'status',None)!='completed' or response.output_parsed is None:
            raise RuntimeError(f'{stage} response refused, incomplete or unparseable; no measurement diagnosis substituted')
        return response.output_parsed

    def propose_rois(self, metadata, images, correction=None):
        return self._call('roi',{'videos':metadata,'coordinate_units':'normalized_0_to_1','correction':correction},images,ROIPlan)

    def diagnose(self, evidence, images):
        return self._call('diagnosis',evidence,images,Diagnosis)

class ReplayProvider:
    """Use recorded/fixture responses. Never presented as live model inference."""
    mode='offline_replay'
    def __init__(self, roi_json, diagnosis_json):
        self.roi_json=Path(roi_json);self.diagnosis_json=Path(diagnosis_json);self.calls=[]
    def propose_rois(self, metadata, images, correction=None):
        self.calls.append({'stage':'roi','source':'replay'})
        return ROIPlan.model_validate_json(self.roi_json.read_text(encoding='utf-8'))
    def diagnose(self, evidence, images):
        self.calls.append({'stage':'diagnosis','source':'replay'})
        return Diagnosis.model_validate_json(self.diagnosis_json.read_text(encoding='utf-8'))


def _jpeg_b64(path, max_side):
    """Downscale to the long side Claude processes natively (saves tokens), JPEG-encode, base64."""
    import cv2
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f'Cannot read image for the model: {Path(path).name}')
    h, w = image.shape[:2]; scale = min(1.0, max_side / max(h, w))
    if scale < 1:
        image = cv2.resize(image, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise ValueError('JPEG encoding failed')
    return base64.b64encode(buf.tobytes()).decode()


class AnthropicProvider:
    """Claude via the Messages API with JSON-schema structured outputs.

    Output is validated again with the strict pydantic contracts; a ValidationError is a ValueError,
    so the workflow's existing one-shot repair loops handle malformed answers.
    """
    mode = 'claude_live'
    DEFAULT_MODEL = 'claude-sonnet-5-5'

    def __init__(self, model=None, client=None, max_image_side=1568, max_tokens=8000):
        self.model = model or os.environ.get('DEVDAY_CLAUDE_MODEL', self.DEFAULT_MODEL)
        if client is None:
            if not os.environ.get('ANTHROPIC_API_KEY'):
                raise RuntimeError('Set ANTHROPIC_API_KEY; use replay/demo for offline verification')
            import anthropic
            client = anthropic.Anthropic(timeout=300, max_retries=2)
        self.client = client; self.max_image_side = max_image_side; self.max_tokens = max_tokens
        self.calls = []

    def _call(self, stage, payload, images, schema):
        from anthropic import transform_schema
        content = [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False, allow_nan=False)}]
        for label, path in images:
            content.append({'type': 'text', 'text': label})
            content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': _jpeg_b64(path, self.max_image_side)}})
        prompt = (Path(__file__).parent / 'prompts' / f'{stage}.txt').read_text(encoding='utf-8')
        response = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=prompt,
            messages=[{'role': 'user', 'content': content}],
            output_config={'format': {'type': 'json_schema', 'schema': transform_schema(schema)}})
        usage = getattr(response, 'usage', None)
        self.calls.append({'stage': stage, 'response_id': getattr(response, 'id', None), 'model': self.model,
                           'stop_reason': getattr(response, 'stop_reason', None),
                           'usage': usage.model_dump() if hasattr(usage, 'model_dump') else None})
        if response.stop_reason in ('refusal', 'max_tokens'):
            raise RuntimeError(f'{stage} response {response.stop_reason}; no measurement diagnosis substituted')
        text = ''.join(getattr(block, 'text', '') for block in response.content if getattr(block, 'type', '') == 'text')
        try:
            return schema.model_validate_json(text)
        except ValueError as exc:  # keep the model's own answer so the repair request can show it
            raise ValueError(f'{exc}\n--- previous answer ---\n{text[:12000]}') from exc

    def propose_rois(self, metadata, images, correction=None):
        return self._call('roi', {'videos': metadata, 'coordinate_units': 'normalized_0_to_1', 'correction': correction}, images, ROIPlan)

    def diagnose(self, evidence, images):
        return self._call('diagnosis', evidence, images, Diagnosis)


def make_provider(kind='claude', model=None):
    """'claude' (Anthropic) or 'openai'."""
    if kind == 'claude':
        return AnthropicProvider(model=model)
    if kind == 'openai':
        return OpenAIProvider(model=model)
    raise ValueError(f'Unknown provider {kind!r}; use claude or openai')
