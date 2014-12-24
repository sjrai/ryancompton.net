# -*- coding: utf-8 -*-
import soundcloud
import praw
import datetime
import logging
import requests
import sys
import pytz
import isoweek
import spotipy
import spotipy.util
import re
import time

import warnings
#soundcloud sends tons of these..
warnings.filterwarnings("ignore", category=ResourceWarning)

FORMAT = '%(asctime)-15s %(levelname)-6s %(message)s'
DATE_FORMAT = '%b %d %H:%M:%S'
formatter = logging.Formatter(fmt=FORMAT, datefmt=DATE_FORMAT)

handler = logging.StreamHandler()
handler.setFormatter(formatter)
fhandler = logging.FileHandler('/home/ubuntu/hearddit.log')
fhandler.setFormatter(formatter)

logger = logging.getLogger(__name__)
logger.addHandler(handler)
logger.addHandler(fhandler)
logger.setLevel(logging.INFO)

def get_submissions(subreddit='electronicmusic',limit=100,session=None):
    if not session:
        r = praw.Reddit(user_agent='get_top; subreddit={0}'.format(subreddit))
    else:
        r = session
    sr = r.get_subreddit(subreddit).get_hot(limit=limit)
    return sr

def submit_link(creds_file, subreddit, title, playlist_url, username='heardditbot'):
    with open(creds_file,'r') as fin:
        d = dict( l.rstrip().split('=') for l in fin)
    r = praw.Reddit(user_agent='post_link; subreddit={0}'.format(subreddit))
    r.login(username=username,password=d[username])
    assert r.is_logged_in()
    
    # identify a daily discussion (I'm banned from /r/electronicmusic...)
    def get_daily_discussion_thread(subreddit,session=None):
        sr = get_submissions(subreddit=subreddit,limit=200,session=session)
        dds = [s for s in sr if "Daily Discussion" in s.title]
        if dds:
            dds = sorted(dds, key=lambda x: -x.created)
            return dds[0]
        else:
            return None

    #if there's a Daily Discussion (eg /r/electronicmusic) use that
    dd = get_daily_discussion_thread(subreddit,session=r)
    if dd:
        dd.add_comment(title + ' ' + playlist_url)
    
    r.submit(subreddit,title=title,url=playlist_url)
    return

def soundclound_login():
    with open('/home/ubuntu/soundcloud_creds.properties','r') as fin:
        d = dict( l.rstrip().split('=') for l in fin)
    client = soundcloud.Client(client_id=d['client_id'], 
                            client_secret=d['client_secret'],
                            username=d['username'],
                           password=d['password'])
    return client

def create_soundcloud_playlist_from_urls(urls, playlist_name):
    """
    login to soundcloud and create a playlist on my account 
    """
    client = soundclound_login()    

    #use soundcloud api to resolve links
    tracks = []
    for url in urls:
        if 'soundcloud' in url:
            logger.info(url)
            try:
                tracks.append(client.get('/resolve', url=url))
            except requests.exceptions.HTTPError:
                logger.error('except!'+url)
    track_ids = [x.id for x in tracks]
    track_dicts = list(map(lambda id: dict(id=id), track_ids))
    logger.info(track_dicts)

    #check if playlist already exists
    my_playlists = client.get('/me/playlists')
    old_list_urls = [p for p in my_playlists if p.fields()['title'] == playlist_name]
    if old_list_urls:
        # add tracks to playlist
        old_list_url = old_list_urls[0]
        client.put(old_list_url.uri, playlist={'tracks': track_dicts})
    else:
        # create the playlist
        client.post('/playlists', playlist={
            'title': playlist_name,
            'sharing': 'public',
            'tracks': track_dicts})

    #get the link to the list created
    my_playlists = client.get('/me/playlists')
    new_list_url = [p.fields()['permalink_url'] for p in my_playlists 
                    if p.fields()['title'] == playlist_name]

    if new_list_url:
        return new_list_url[0]
    else:
        logger.warning('no new soundcloud list')
    return

def spotify_login():
    scopes = 'playlist-modify-public'
    with open('/home/ubuntu/my_spotify_api_key.properties','r') as fin:
        d = dict( l.rstrip().split('=') for l in fin)
    token = spotipy.util.prompt_for_user_token(username='1210400091',
        scope=scopes,
        client_id=d['SPOTIPY_CLIENT_ID'], 
        client_secret=d['SPOTIPY_CLIENT_SECRET'],
        redirect_uri=d['SPOTIPY_REDIRECT_URI']
        )
    logger.warning('got a token!!!!: {}'.format(token))

    return spotipy.Spotify(auth=token)

def search_spotify_for_a_title(title, sp):
    query = re.split('(\[|\()',title)[0] # title.split('[')[0]
    
    if len(query) > 5:
        results = sp.search(q=query, type='track')
        if len(results['tracks']['items']) > 0:
            logger.info('hit! {0}'.format(query))
            return results
        else:
            logger.info('miss! {0}'.format(query))
    return

def create_spotify_playlist_from_titles(todays_titles, playlist_name):
    """
    login to spotify, search for titles, and create a playlist
    """
    sp = spotify_login()

    #try to map the submission titles to spotify tracks
    search_results = [search_spotify_for_a_title(x,sp) for x in todays_titles]
    search_results = [x for x in search_results if x and (len(x['tracks']['items']) > 0)]
    hits = [x['tracks']['items'][0] for x in search_results]

    #get all my playlists, check if playlist_name already there
    new_pl = None
    for my_pl in sp.user_playlists(user=sp.me()['id'])['items']:
        logger.info('my_pl: {}'.format(my_pl['name']))
        if my_pl['name'] == playlist_name:
            logger.warning('appending to {}'.format(my_pl))
            new_pl = sp.user_playlist(user=sp.me()['id'], playlist_id=my_pl['id'])
            break
    if not new_pl:
        logger.warning('new playlist!')
        new_pl = sp.user_playlist_create(user=sp.me()['id'],name=playlist_name,public=True)

    out_url = new_pl['external_urls']['spotify']
    logger.info(out_url)

    #get all tracks in the playlist
    new_new_pl = sp.user_playlist(sp.me()['id'], new_pl['uri'])

    old_track_uris = set([x['track']['uri'] for x in new_new_pl['tracks']['items']])
    for s in old_track_uris:
        logger.debug('old! {}'.format(s))

    new_track_uris = [hit['uri'] for hit in hits if hit['uri'] not in old_track_uris]
    for new_track_uri in new_track_uris:
        logger.debug('new! {}'.format(new_track_uri))

    #can only insert 100 tracks at a time...
    #http://stackoverflow.com/a/434328/424631
    def chunker(seq, size):
        return (seq[pos:pos + size] for pos in range(0, len(seq), size))

    logger.warning('adding {0} new tracks to {1}'.format(len(new_track_uris), new_pl['name']))
    if new_track_uris > 0:
        for sublist in chunker(new_track_uris,99):
            sp.user_playlist_add_tracks(sp.me()['id'],new_pl['uri'],sublist)
            time.sleep(7)
            logger.warning('added {0} new tracks to {1}'.format(len(sublist), new_pl['name']))

    return out_url


def main():
    #today = datetime.datetime.now(pytz.timezone('US/Pacific')).date()
    monday = isoweek.Week(2015,0).thisweek().monday()

    subreddit=sys.argv[1]
    playlist_name = '/r/'+subreddit+' week of '+str(monday)
    botname='heardditbot'
    logger.info(subreddit + "\t" + playlist_name)

    #figure all the hot submissions on the target subreddit
    hot_links = list(get_submissions(subreddit=subreddit,limit=500))
    logger.info('number of submissions: {0}'.format(len(hot_links)))

    #parse out the urls, titles, and post times from the submissions 
    urls = [(x.url, x.title, datetime.datetime.fromtimestamp(x.created)) for x in hot_links]
    todays_urls = [x[0] for x in urls if x[2].date() >= monday]
    todays_titles = [x[1] for x in urls if x[2].date() >= monday]
    logger.info("len(todays_urls): {0}".format(len(todays_urls)))
    logger.info("len(todays_titles): {0}".format(len(todays_titles)))

    new_soundcloud_list_url = create_soundcloud_playlist_from_urls(todays_urls, playlist_name)
    logger.info(new_soundcloud_list_url)

    new_spotify_list_url = create_spotify_playlist_from_titles(todays_titles, playlist_name)
    logger.info(new_spotify_list_url)

    #link_title='Soundcloud playlist for '+playlist_name
    #logger.info('posting '+link_title+' to '+subreddit+' url: '+new_list_url)
    #submit_link(creds_file='/home/ubuntu/my_reddit_accounts.properties', subreddit=subreddit, 
    #            title=link_title, playlist_url=new_list_url, username=botname)

    return

if __name__ == '__main__':
    main()