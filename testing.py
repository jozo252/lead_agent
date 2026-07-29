

def strip_legal_suffix(name):
    suffixes = [
    "s.r.o.",
    "s. r. o.",
    "a.s.",
    "a. s.",
]
    name = name.strip()

    for suffix in suffixes:
        if name.lower().endswith(suffix):
            name = name[:-len(suffix)].strip()
            break

    return name

print(strip_legal_suffix("ABC Elektro s. r.o."))

raw_data = {
    "web": {
        "results": [
            {
                "title": "ABC Elektro",
                "url": "https://abcelektro.sk",
                "description": "Elektroinštalačné práce"
            },
            {
                "title": "ABC Elektro Facebook",
                "url": "https://facebook.com/abcelektro"
            }
        ]
    }
}


def normalize_search_results(raw_data):
    if not isinstance(raw_data,dict):
        raw_data={}
    web=raw_data.get("web")
    if not isinstance(web,dict):
        web={}
    results=web.get("results")
    normalized = []

    for result in results:
        if not isinstance(result, dict):
            continue

        url = result.get("url")

        if not url:
            continue

        normalized_result = {
            "title": result.get("title"),
            "url": url,
            "description": result.get("description"),
        }

        normalized.append(normalized_result)

    return normalized

print(normalize_search_results(raw_data))