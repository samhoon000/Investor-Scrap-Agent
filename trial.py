from scraper import get_yc_company

text = get_yc_company("openai")

print(text[:3000])