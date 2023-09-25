import requests
from bs4 import BeautifulSoup, Tag, NavigableString
import re

def scrape_webpage(url: str):
    response = requests.get(url)
    results = []

    if response.status_code == 200:
        soup = BeautifulSoup(response.text, 'html.parser')
        date = None

        ul_tag = soup.find('ul')
        if ul_tag:
            ul_tags = ul_tag.findAll('ul')
            for ul in ul_tags:
                li_tags = ul.find('li')
                for i in li_tags:
                    print(i)
                    print("separator")
                    if isinstance(i, Tag):
                        name_anchor = i.find('a', {'name': True})
                        href_anchor = i.find('a', {'href': True})
                        if name_anchor:
                            date = name_anchor.find('b').get_text().strip()
                        elif href_anchor:
                            first_href = href_anchor['href']
                            if first_href.startswith('by-club'):
                                event_type = 'by-club'
                            elif first_href.startswith('by-date'):
                                event_type = 'by-date'
                            else:
                                event_type = 'unknown'

                            if event_type == 'by-club':
                                by_band_hrefs = i.find_all('a', {'href': re.compile(r'^by-band')})
                                by_club_href = i.find('a', {'href': re.compile(r'^by-club')})
                                for by_band_href in by_band_hrefs:
                                    by_band_text = by_band_href.get_text()
                                    results.append((date, by_band_text, by_club_href))
                                    #print(date, by_band_text, by_club_href)
                    elif isinstance(i, NavigableString):
                        continue
    else:
        print(f"Failed to retrieve the web page. Status code: {response.status_code}")
    return results

url = 'http://www.foopee.com/punk/the-list/by-date.9.html'
scrape_webpage(url)
