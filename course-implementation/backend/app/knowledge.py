import math
import re
from collections import Counter

from .schema import digest

ALIASES = {
    "服务": ("微服务",), "api": ("接口", "端点"), "发送": ("发布", "生产"),
    "调用": ("依赖",), "入口": ("上游",), "队列": ("mq", "消息"),
}


def terms(question):
    text = question.casefold()
    tokens = re.findall(r"[a-z0-9_./-]+|[\u3400-\u9fff]+", text)
    words = []
    for token in tokens:
        if token[0].isascii():
            words.append(token)
            words.extend(re.findall(r"[a-z]+|\d+", token))
        else:
            words.extend(token[i:i + 2] for i in range(len(token) - 1))
    for main, alternatives in ALIASES.items():
        if any(value in text for value in (main, *alternatives)):
            words.append(main)
    return Counter(words)


def retrieve(question, examples, level, limit=3):
    candidates = [example for example in examples if example["review"] == "approved"]
    if not candidates or not limit:
        return []
    vectors = [terms(example["question"]) for example in candidates]
    document_frequency = Counter(word for vector in vectors for word in vector)
    idf = {word: 1 + math.log((len(vectors) + 1) / (freq + 1))
           for word, freq in document_frequency.items()}
    target = terms(question)

    def weights(vector):
        return {word: (1 + math.log(count)) * idf[word]
                for word, count in vector.items() if word in idf}

    query = weights(target)
    query_norm = math.sqrt(sum(value ** 2 for value in query.values()))

    def score(index):
        vector = weights(vectors[index])
        norm = math.sqrt(sum(value ** 2 for value in vector.values()))
        cosine = (sum(query.get(word, 0) * weight for word, weight in vector.items()) / (query_norm * norm)
                  if query_norm and norm else 0)
        return cosine * (1 if candidates[index]["level"] == level else 0.85)

    order = sorted(range(len(candidates)), key=lambda i: (-score(i), candidates[i]["id"]))
    return [candidates[i] for i in order[:limit] if score(i) > 0]


def version(examples):
    return digest([(item["id"], item["version"], item["review"], item["question"], item["plan"])
                   for item in examples])
