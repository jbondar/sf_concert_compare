import requests
from bs4 import BeautifulSoup
from spotipy.oauth2 import SpotifyOAuth
import spotipy
import openpyxl
from typing import List, Tuple

# Spotify API credentials
CLIENT_ID = 'a8b9a6755a614462a842b3ecd9fe1da1'
CLIENT_SECRET = 'cd556e97124d436cbb1ad653b1a39dcc'
REDIRECT_URI = "https://localhost:888/callback"
SCOPE = 'user-library-read user-read-recently-played'

# Initialize Spotipy with user authentication
sp = spotipy.Spotify(auth_manager=SpotifyOAuth(client_id=CLIENT_ID,
                                               client_secret=CLIENT_SECRET,
                                               redirect_uri=REDIRECT_URI,
                                               scope=SCOPE))

# Fetch the authenticated user's data
user_data = sp.me()

# Extract the user's Spotify username (user ID)
user_id = user_data['id']

def get_artist_events_from_url(url: str) -> List[Tuple[str, str, str]]:
    """
    Scrapes events from the given URL and returns a list of tuples
    containing band name, venue, and date.
    """
    try:
        response = requests.get(url)
        response.raise_for_status()  # Check if the request was successful
    except requests.RequestException as e:
        print(f"Error: Unable to fetch data from {url}. {str(e)}")
        return []

    soup = BeautifulSoup(response.text, 'html.parser')
    
    month = None
    day = None
    events = []

    for tr in soup.find_all('tr'):
        th = tr.find('th')
        if th:
            month = th.get_text(strip=True).split(' ')[0].lower()
            continue

        if tr.get('valign') == 'top':
            date_td = tr.find('td', {'bgcolor': '#CCCC00'})
            if date_td:
                b_tag = date_td.find('b')
                if b_tag:
                    # Splitting date text into weekday and day
                    weekday, day = b_tag.get_text(strip=True, separator=' ').split(' ')
                    # Constructing the date string as per your format
                    formatted_date = f"{weekday} {month} {day}"
                    date = formatted_date
            bands_td = tr.find('td', {'bgcolor': '#CCCCCC'})
            if bands_td:
                bands = bands_td.stripped_strings
                bands = [band.strip() for band in bands if band.strip()]
                venue_td = bands_td.find_next_sibling('td')
                venue = venue_td.get_text(strip=True) if venue_td else ''
                date = f"{month} {day}"
                for band in bands:
                    events.append((band, venue, date))
    
    return events


# Function to get all artists from Spotify
def get_all_spotify_artists() -> List[str]:
    """Function to get all artists from Spotify"""
    artists = set()  # Use a set to avoid duplicates

    # Fetch the user's recently played tracks with pagination
    recent_tracks = sp.current_user_recently_played(limit=50)  # Initial fetch with 50 tracks

    while recent_tracks:
        for track in recent_tracks['items']:
            track_info = track['track']
            for artist in track_info['artists']:
                artists.add(artist['name'].upper())

        # Check for next page
        if recent_tracks['next']:
            recent_tracks = sp.next(recent_tracks)
        else:
            break

    # Fetch the user's playlists
    playlists = sp.current_user_playlists()

    # Iterate through the user's playlists
    for playlist in playlists['items']:
        playlist_id = playlist['id']

        # Fetch the tracks in the playlist with pagination
        results = sp.playlist_tracks(playlist_id, limit=100)  # Adjust the limit as needed

        while results:
            for track in results['items']:
                track_info = track['track']
                for artist in track_info['artists']:
                    artists.add(artist['name'].upper())

            # Check for next page
            if results['next']:
                results = sp.next(results)
            else:
                break

    # Fetch the user's saved tracks
    saved_tracks = sp.current_user_saved_tracks()

    for item in saved_tracks['items']:
        track_info = item['track']
        for artist in track_info['artists']:
            artists.add(artist['name'].upper())

    # Convert the set of artists to a list
    artist_list = list(artists)
    return artist_list

def compare_artists(html_artists_data: List[Tuple[str, str, str]], spotify_artists):
    """Function to compare artists between HTML data and Spotify library"""
    
    # Convert the Spotify artist names to uppercase for case-insensitive comparison
    spotify_artists_upper = [artist.upper() for artist in spotify_artists]
    common_artists_data = []

    for artist_data in html_artists_data:
        if len(artist_data) >= 3:  # Ensure the tuple has at least 3 elements
            artist_name, venue, date = artist_data[:3]  # Unpack the first 3 elements
            artist_name_upper = artist_name.upper()
            if artist_name_upper in spotify_artists_upper:
                etc = artist_data[3] if len(artist_data) > 3 else None
                common_artists_data.append((artist_name, venue, date, etc))

    return common_artists_data

def write_to_excel(data: List[Tuple[str, str, str]], filename: str = "common_artists.xlsx"):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Common Artists"
    
    # Writing Headers to Excel File
    sheet["A1"] = "Artist"
    sheet["B1"] = "Venue"
    sheet["C1"] = "Date"
    
    # Writing Data to Excel File
    for index, artist_data in enumerate(data, start=2): # start=2 to avoid overwriting headers
        artist, venue, date, *_ = artist_data  # Use * to capture any additional elements in the tuple
        sheet[f"A{index}"] = artist
        sheet[f"B{index}"] = venue
        sheet[f"C{index}"] = date
        
    # Save the Excel File
    workbook.save(filename)
    print(f"Data has been written to {filename}")


if __name__ == "__main__":
    base_url = "http://jon.luini.com/thelist/date.html"

    html_artist_data = []
    artist_date_data = get_artist_events_from_url(base_url)
    html_artist_data.extend(artist_date_data)

    spotify_artist_data = get_all_spotify_artists()
    print(f"Number of artists found for user {user_id}: {len(spotify_artist_data)}")

    common_artists = compare_artists(html_artist_data, spotify_artist_data)
    
    # If common artists are found, write them to Excel
    if common_artists:
        write_to_excel(common_artists)
    else:
        print("No common artists found.")

else:
    print("No valid page numbers found on the website.")
