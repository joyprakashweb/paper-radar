"""Score candidate papers for relevance with a free NVIDIA-hosted model."""
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

URL = "https://integrate.api.nvidia.com/v1/chat/completions"
CATEGORIES = [
    "Electroosmosis & electrophoresis",
    "Diffusio-/chemi-osmosis & phoresis",
    "Thermal effects (thermophoresis, thermo-electro)",
    "Ion transport in nanopores & nanochannels",
    "Energy harvesting & reverse electrodialysis",
    "Desalination & electrodialysis",
    "DNA sequencing & nanopore sensing",
    "Biomedical & lab-on-chip devices",
    "Other",
]


def log(*a):
    print("[ai]", *a, flush=True)


def _system_prompt(topic):
    cats = "\n".join(f"- {c}" for c in CATEGORIES)
    return f"""You screen newly published scientific papers for one researcher.

RESEARCHER'S TOPIC:
{topic['description'].strip()}

NOT WANTED (score these 0-2 even if keywords match):
{topic.get('exclude', '').strip()}

For every paper, give:
- "score": integer 0-10 for how useful it is to THIS researcher.
  9-10 = directly on their topic (new theory, simulation or experiment on
  electrokinetic / diffusio- / thermo-driven transport in micro/nano channels
  or pores, or its applications they listed).
  6-8 = clearly related and worth reading.
  3-5 = loosely related.
  0-2 = off-topic or only shares a keyword.
- "category": exactly one of:
{cats}
- "reason": ONE short sentence (max 25 words) saying what the paper does and why
  it matters to this researcher. No hype.

Reply with ONLY a JSON object, no markdown, in this form:
{{"results": [{{"id": "p1", "score": 7, "category": "...", "reason": "..."}}]}}
Include every paper id you were given."""


def _paper_block(i, p):
    abstract = (p.get("abstract") or "(no abstract available)")[:1400]
    return {"id": f"p{i}", "title": p["title"], "venue": p.get("venue", ""), "abstract": abstract}


def _parse(content):
    content = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
    content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
    a, b = content.find("{"), content.rfind("}")
    if a < 0 or b < 0:
        raise ValueError("no JSON in reply")
    data = json.loads(content[a:b + 1])
    return data.get("results", data if isinstance(data, list) else [])


class Scorer:
    def __init__(self, api_key, cfg_ai, topic):
        self.key = api_key
        self.models = list(cfg_ai["models"])
        self.batch = int(cfg_ai.get("batch_size", 8))
        self.parallel = int(cfg_ai.get("parallel_requests", 3))
        self.system = _system_prompt(topic)
        self.lock = threading.Lock()
        self.model_idx = 0
        self.used_model = None
        self.calls = 0
        self.failed_batches = 0

    def _call(self, model, papers):
        msgs = [{"role": "system", "content": self.system},
                {"role": "user", "content": "Papers:\n" + json.dumps(
                    [_paper_block(i, p) for i, p in enumerate(papers)], ensure_ascii=False)}]
        body = {"model": model, "messages": msgs, "temperature": 0.1, "max_tokens": 6000}
        r = requests.post(URL, json=body, timeout=180,
                          headers={"Authorization": f"Bearer {self.key}", "Accept": "application/json"})
        with self.lock:
            self.calls += 1
        if r.status_code == 429:
            raise RuntimeError("rate-limited")
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        msg = r.json()["choices"][0]["message"]
        return _parse(msg.get("content") or "")

    def _score_batch(self, papers):
        attempts = 0
        while attempts < 2 * len(self.models) + 2:
            with self.lock:
                if self.model_idx >= len(self.models):
                    return {}
                model = self.models[self.model_idx]
            try:
                results = self._call(model, papers)
                out = {}
                for res in results:
                    m = re.match(r"p(\d+)$", str(res.get("id", "")))
                    if not m or int(m.group(1)) >= len(papers):
                        continue
                    cat = res.get("category") if res.get("category") in CATEGORIES else "Other"
                    out[papers[int(m.group(1))]["key"]] = {
                        "score": max(0, min(10, int(float(res.get("score", 0))))),
                        "category": cat,
                        "reason": str(res.get("reason", ""))[:300],
                    }
                with self.lock:
                    self.used_model = model
                return out
            except Exception as e:
                attempts += 1
                msg = str(e)
                if "rate-limited" in msg:
                    time.sleep(20)
                    continue
                log(f"{model}: {msg[:160]}")
                if attempts % 2 == 0:  # two failures on this model -> switch
                    with self.lock:
                        if self.models[self.model_idx] == model:
                            self.model_idx += 1
                            if self.model_idx < len(self.models):
                                log("switching to", self.models[self.model_idx])
                time.sleep(3)
        return {}

    def score(self, papers):
        batches = [papers[i:i + self.batch] for i in range(0, len(papers), self.batch)]
        log(f"scoring {len(papers)} papers in {len(batches)} requests")
        scores = {}
        done = 0
        with ThreadPoolExecutor(self.parallel) as ex:
            for res in ex.map(self._score_batch, batches):
                if not res:
                    self.failed_batches += 1
                scores.update(res)
                done += 1
                if done % 20 == 0:
                    log(f"  {done}/{len(batches)} requests done")
        log(f"scored {len(scores)}/{len(papers)} with {self.used_model}; {self.calls} API calls")
        return scores
