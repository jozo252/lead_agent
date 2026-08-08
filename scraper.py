from scrapegraphai.graphs import SmartScraperGraph
from dotenv import load_dotenv
import json
import os

load_dotenv()

graph_config = {
    "llm": {
        "api_key": os.environ["OPENAI_API_KEY"],
        "model": "openai/gpt-4o-mini",
    },
    "verbose": True,
    "headless": True,
    "timeout": 30,
}

graph = SmartScraperGraph(
    prompt="Extract one book with fields: name, price and product_url.",
    source="https://books.toscrape.com/",
    config=graph_config,
)

result = graph.run()

print(json.dumps(result, indent=4, ensure_ascii=False))