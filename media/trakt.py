import time

import backoff
import requests

from misc.log import logger

log = logger.get_logger(__name__)


def backoff_handler(details):
    log.warning("Backing off {wait:0.1f} seconds afters {tries} tries "
                "calling function {target} with args {args} and kwargs "
                "{kwargs}".format(**details))


class Trakt:
    non_user_lists = ['anticipated', 'trending', 'popular', 'boxoffice']

    def __init__(self, cfg):
        self.cfg = cfg
        self.api_key = cfg.trakt.api_key
        self.api_secret = cfg.trakt.api_secret
        self.headers = {
            'Content-Type': 'application/json',
            'trakt-api-version': '2',
            'trakt-api-key': self.api_key
        }

    def validate_api_key(self):
        try:
            # request trending shows to determine if api_key is valid
            payload = {'extended': 'full', 'limit': 1000}

            # make request
            req = requests.get(
                'https://api.trakt.tv/shows/anticipated',
                headers=self.headers,
                params=payload,
                timeout=30
            )
            log.debug("Request Response: %d", req.status_code)

            if req.status_code == 200:
                return True
            return False
        except Exception:
            log.exception("Exception validating api_key: ")
        return False

    ############################################################
    # OAuth Authentication Initialisation
    ############################################################

    def __oauth_request_device_code(self):
        log.info("We're talking to Trakt to get your verification code. Please wait a moment...")

        payload = {'client_id': self.api_key}

        # Request device code
        req = requests.post('https://api.trakt.tv/oauth/device/code', params=payload, headers=self.headers)
        device_code_response = req.json()

        # Display needed information to the user
        log.info('Go to: %s on any device and enter %s. We\'ll be polling Trakt every %s seconds for a reply',
                 device_code_response['verification_url'], device_code_response['user_code'],
                 device_code_response['interval'])

        return device_code_response

    def __oauth_process_token_request(self, req):
        success = False

        if req.status_code == 200:
            # Success; saving the access token
            access_token_response = req.json()
            access_token = access_token_response['access_token']

            # But first we need to find out what user this token belongs to
            temp_headers = self.headers
            temp_headers['Authorization'] = 'Bearer ' + access_token

            req = requests.get('https://api.trakt.tv/users/me', headers=temp_headers)

            from misc.config import Config
            new_config = Config()

            new_config.merge_settings({
                "trakt": {
                    req.json()['username']: access_token_response
                }
            })

            success = True
        elif req.status_code == 404:
            log.debug('The device code was wrong')
            log.error('Whoops, something went wrong; aborting the authentication process')
        elif req.status_code == 409:
            log.error('You\'ve already authenticated this application; aborting the authentication process')
        elif req.status_code == 410:
            log.error('The authentication process has expired; please start again')
        elif req.status_code == 418:
            log.error('You\'ve denied the authentication; are you sure? Please try again')
        elif req.status_code == 429:
            log.debug('We\'re polling too quickly.')

        return success, req.status_code

    def __oauth_poll_for_access_token(self, device_code, polling_interval=5, polling_expire=600):
        polling_start = time.time()
        time.sleep(polling_interval)
        tries = 0

        while time.time() - polling_start < polling_expire:
            tries += 1

            log.debug('Polling Trakt for the %sth time; %s seconds left', tries,
                      polling_expire - round(time.time() - polling_start))

            payload = {'code': device_code, 'client_id': self.api_key, 'client_secret': self.api_secret,
                       'grant_type': 'authorization_code'}

            # Poll Trakt for access token
            req = requests.post('https://api.trakt.tv/oauth/device/token', params=payload, headers=self.headers)

            success, status_code = self.__oauth_process_token_request(req)

            if success:
                break
            elif status_code == 426:
                log.debug('Increasing the interval by one second')
                polling_interval += 1

            time.sleep(polling_interval)
        return False

    def __oauth_refresh_access_token(self, refresh_token):
        # TODO Doesn't work

        payload = {'refresh_token': refresh_token, 'client_id': self.api_key, 'client_secret': self.api_secret,
                   'grant_type': 'refresh_token'}

        req = requests.post('https://api.trakt.tv/oauth/token', params=payload, headers=self.headers)

        success, status_code = self.__oauth_process_token_request(req)

        return success

    def oauth_authentication(self):
        try:
            device_code_response = self.__oauth_request_device_code()

            if self.__oauth_poll_for_access_token(device_code_response['device_code'],
                                                  device_code_response['interval'],
                                                  device_code_response['expires_in']):
                return True
        except Exception:
            log.exception("Exception occurred when authenticating user")
        return False

    def oauth_headers(self, user):
        headers = self.headers

        if user is None or user not in self.cfg['trakt'].keys():
            log.debug('No authenticated user corresponds to "%s", so the first user in the config to authenticated.', user)

            users = self.cfg['trakt']
            
            if 'api_key' in users.keys():
                users.pop('api_key')

            if 'api_secret' in users.keys():
                users.pop('api_secret')

            user = list(users.keys())[0]

        token_information = self.cfg['trakt'][user]
        # Check if the acces_token for the user is expired
        expires_at = token_information['created_at'] + token_information['expires_in']

        if expires_at < round(time.time()):
            log.info("The access token for the user %s has expired. We're requesting a new one; please wait a moment.",
                     user)

            if self.__oauth_refresh_access_token(token_information["refresh_token"]):
                log.info("The access token for the user %s has been refreshed. Please restart the application.",
                         user)

        headers['Authorization'] = 'Bearer ' + token_information['access_token']

        return headers

    ############################################################
    # Shows
    ############################################################

    @backoff.on_predicate(backoff.expo, lambda x: x is None, max_tries=4, on_backoff=backoff_handler)
    def get_anticipated_shows(self, limit=1000, languages=None):
        try:
            processed_shows = []

            if languages is None:
                languages = ['en']

            # generate payload
            payload = {'extended': 'full', 'limit': limit, 'page': 1}
            if languages:
                payload['languages'] = ','.join(languages)

            # make request
            while True:
                req = requests.get(
                    'https://api.trakt.tv/shows/anticipated',
                    headers=self.headers,
                    params=payload,
                    timeout=30
                )
                log.debug("Request URL: %s", req.url)
                log.debug("Request Payload: %s", payload)
                log.debug("Response Code: %d", req.status_code)
                log.debug("Response Page: %d of %d", payload['page'],
                          0 if 'X-Pagination-Page-Count' not in req.headers else int(
                              req.headers['X-Pagination-Page-Count']))

                if req.status_code == 200:
                    resp_json = req.json()

                    for show in resp_json:
                        if show not in processed_shows:
                            processed_shows.append(show)

                    # check if we have fetched the last page, break if so
                    if 'X-Pagination-Page-Count' not in req.headers or not int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There was no more pages to retrieve")
                        break
                    elif payload['page'] >= int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There are no more pages to retrieve results from")
                        break
                    else:
                        log.info("There are %d pages left to retrieve results from",
                                 int(req.headers['X-Pagination-Page-Count']) - payload['page'])
                        payload['page'] += 1
                        time.sleep(5)

                else:
                    log.error("Failed to retrieve anticipated shows, request response: %d", req.status_code)
                    break

            if len(processed_shows):
                log.debug("Found %d anticipated shows", len(processed_shows))
                return processed_shows
            return None
        except Exception:
            log.exception("Exception retrieving anticipated shows: ")
        return None

    @backoff.on_predicate(backoff.expo, lambda x: x is None, max_tries=4, on_backoff=backoff_handler)
    def get_watchlist_shows(self, user=None, limit=1000, languages=None):
        try:
            processed_shows = []

            if languages is None:
                languages = ['en']

            # generate payload
            payload = {'extended': 'full', 'limit': limit, 'page': 1}
            if languages:
                payload['languages'] = ','.join(languages)

            # make request
            while True:
                req = requests.get('https://api.trakt.tv/sync/watchlist/shows', params=payload,
                                   headers=self.oauth_headers(user),
                                   timeout=30)
                log.debug("Request User: %s", user)
                log.debug("Request URL: %s", req.url)
                log.debug("Request Payload: %s", payload)
                log.debug("Response Code: %d", req.status_code)
                log.debug("Response Page: %d of %d", payload['page'],
                          0 if 'X-Pagination-Page-Count' not in req.headers else int(
                              req.headers['X-Pagination-Page-Count']))

                if req.status_code == 200:
                    resp_json = req.json()

                    for show in resp_json:
                        if show not in processed_shows:
                            processed_shows.append(show)

                    # check if we have fetched the last page, break if so
                    if 'X-Pagination-Page-Count' not in req.headers or not int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There was no more pages to retrieve")
                        break
                    elif payload['page'] >= int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There are no more pages to retrieve results from")
                        break
                    else:
                        log.info("There are %d pages left to retrieve results from",
                                 int(req.headers['X-Pagination-Page-Count']) - payload['page'])
                        payload['page'] += 1
                        time.sleep(5)
                elif req.status_code == 401:
                    log.error("The authentication to Trakt is revoked. Please re-authenticate.")

                    exit()
                else:
                    log.error("Failed to retrieve shows on watchlist from %s, request response: %d", user,
                              req.status_code)
                    break

            if len(processed_shows):
                log.debug("Found %d shows on watchlist from %s", len(processed_shows), user)

                return processed_shows
            return None
        except Exception:
            log.exception("Exception retrieving shows on watchlist")
        return None

    @backoff.on_predicate(backoff.expo, lambda x: x is None, max_tries=4, on_backoff=backoff_handler)
    def get_user_list_shows(self, list_id, user=None, limit=1000, languages=None):
        try:
            processed_shows = []

            if languages is None:
                languages = ['en']

            # generate payload
            payload = {'extended': 'full', 'limit': limit, 'page': 1}
            if languages:
                payload['languages'] = ','.join(languages)

            # make request
            while True:
                req = requests.get('https://api.trakt.tv/users/' + user + '/lists/' + list_id + '/items/shows',
                                   params=payload,
                                   headers=self.oauth_headers(user),
                                   timeout=30)
                log.debug("Request User: %s", user)
                log.debug("Request URL: %s", req.url)
                log.debug("Request Payload: %s", payload)
                log.debug("Response Code: %d", req.status_code)
                log.debug("Response Page: %d of %d", payload['page'],
                          0 if 'X-Pagination-Page-Count' not in req.headers else int(
                              req.headers['X-Pagination-Page-Count']))

                if req.status_code == 200:
                    resp_json = req.json()

                    for show in resp_json:
                        if show not in processed_shows:
                            processed_shows.append(show)

                    # check if we have fetched the last page, break if so
                    if 'X-Pagination-Page-Count' not in req.headers or not int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There was no more pages to retrieve")
                        break
                    elif payload['page'] >= int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There are no more pages to retrieve results from")
                        break
                    else:
                        log.info("There are %d pages left to retrieve results from",
                                 int(req.headers['X-Pagination-Page-Count']) - payload['page'])
                        payload['page'] += 1
                        time.sleep(5)
                elif req.status_code == 401:
                    log.error("The authentication to Trakt is revoked. Please re-authenticate.")

                    exit()
                else:
                    log.error("Failed to retrieve shows on watchlist from %s, request response: %d", user,
                              req.status_code)
                    break

            if len(processed_shows):
                log.debug("Found %d shows on watchlist from %s", len(processed_shows), user)

                return processed_shows
            return None
        except Exception:
            log.exception("Exception retrieving shows on watchlist")
        return None

    @backoff.on_predicate(backoff.expo, lambda x: x is None, max_tries=4, on_backoff=backoff_handler)
    def get_trending_shows(self, limit=1000, languages=None):
        try:
            processed_shows = []

            if languages is None:
                languages = ['en']

            # generate payload
            payload = {'extended': 'full', 'limit': limit, 'page': 1}
            if languages:
                payload['languages'] = ','.join(languages)

            # make request
            while True:
                req = requests.get(
                    'https://api.trakt.tv/shows/trending',
                    headers=self.headers,
                    params=payload,
                    timeout=30
                )
                log.debug("Request URL: %s", req.url)
                log.debug("Request Payload: %s", payload)
                log.debug("Response Code: %d", req.status_code)
                log.debug("Response Page: %d of %d", payload['page'],
                          0 if 'X-Pagination-Page-Count' not in req.headers else int(
                              req.headers['X-Pagination-Page-Count']))

                if req.status_code == 200:
                    resp_json = req.json()

                    for show in resp_json:
                        if show not in processed_shows:
                            processed_shows.append(show)

                    # check if we have fetched the last page, break if so
                    if 'X-Pagination-Page-Count' not in req.headers or not int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There was no more pages to retrieve")
                        break
                    elif payload['page'] >= int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There are no more pages to retrieve results from")
                        break
                    else:
                        log.info("There are %d pages left to retrieve results from",
                                 int(req.headers['X-Pagination-Page-Count']) - payload['page'])
                        payload['page'] += 1
                        time.sleep(5)

                else:
                    log.error("Failed to retrieve trending shows, request response: %d", req.status_code)
                    break

            if len(processed_shows):
                log.debug("Found %d trending shows", len(processed_shows))
                return processed_shows
            return None
        except Exception:
            log.exception("Exception retrieving trending shows: ")
        return None

    @backoff.on_predicate(backoff.expo, lambda x: x is None, max_tries=4, on_backoff=backoff_handler)
    def get_popular_shows(self, limit=1000, languages=None):
        try:
            processed_shows = []

            if languages is None:
                languages = ['en']

            # generate payload
            payload = {'extended': 'full', 'limit': limit, 'page': 1}
            if languages:
                payload['languages'] = ','.join(languages)

            # make request
            while True:
                req = requests.get(
                    'https://api.trakt.tv/shows/popular',
                    headers=self.headers,
                    params=payload,
                    timeout=30
                )
                log.debug("Request URL: %s", req.url)
                log.debug("Request Payload: %s", payload)
                log.debug("Response Code: %d", req.status_code)
                log.debug("Response Page: %d of %d", payload['page'],
                          0 if 'X-Pagination-Page-Count' not in req.headers else int(
                              req.headers['X-Pagination-Page-Count']))

                if req.status_code == 200:
                    resp_json = req.json()

                    # process list so it conforms to standard we expect ( e.g. {"show": {.....}} )
                    for show in resp_json:
                        if show not in processed_shows:
                            processed_shows.append({'show': show})

                    # check if we have fetched the last page, break if so
                    if 'X-Pagination-Page-Count' not in req.headers or not int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There was no more pages to retrieve")
                        break
                    elif payload['page'] >= int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There are no more pages to retrieve results from")
                        break
                    else:
                        log.info("There are %d pages left to retrieve results from",
                                 int(req.headers['X-Pagination-Page-Count']) - payload['page'])
                        payload['page'] += 1
                        time.sleep(5)

                else:
                    log.error("Failed to retrieve popular shows, request response: %d", req.status_code)
                    break

            if len(processed_shows):
                log.debug("Found %d popular shows", len(processed_shows))
                return processed_shows
            return None
        except Exception:
            log.exception("Exception retrieving popular shows: ")
        return None

    ############################################################
    # Movies
    ############################################################

    @backoff.on_predicate(backoff.expo, lambda x: x is None, max_tries=4, on_backoff=backoff_handler)
    def get_anticipated_movies(self, limit=1000, languages=None):
        try:
            processed_movies = []

            if languages is None:
                languages = ['en']

            # generate payload
            payload = {'extended': 'full', 'limit': limit, 'page': 1}
            if languages:
                payload['languages'] = ','.join(languages)

            # make request
            while True:
                req = requests.get(
                    'https://api.trakt.tv/movies/anticipated',
                    headers=self.headers,
                    params=payload,
                    timeout=30
                )
                log.debug("Request URL: %s", req.url)
                log.debug("Request Payload: %s", payload)
                log.debug("Response Code: %d", req.status_code)
                log.debug("Response Page: %d of %d", payload['page'],
                          0 if 'X-Pagination-Page-Count' not in req.headers else int(
                              req.headers['X-Pagination-Page-Count']))

                if req.status_code == 200:
                    resp_json = req.json()

                    for movie in resp_json:
                        if movie not in processed_movies:
                            processed_movies.append(movie)

                    # check if we have fetched the last page, break if so
                    if 'X-Pagination-Page-Count' not in req.headers or not int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There was no more pages to retrieve")
                        break
                    elif payload['page'] >= int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There are no more pages to retrieve results from")
                        break
                    else:
                        log.info("There are %d pages left to retrieve results from",
                                 int(req.headers['X-Pagination-Page-Count']) - payload['page'])
                        payload['page'] += 1
                        time.sleep(5)

                else:
                    log.error("Failed to retrieve anticipated movies, request response: %d", req.status_code)
                    break

            if len(processed_movies):
                log.debug("Found %d anticipated movies", len(processed_movies))
                return processed_movies
            return None
        except Exception:
            log.exception("Exception retrieving anticipated movies: ")
        return None

    @backoff.on_predicate(backoff.expo, lambda x: x is None, max_tries=4, on_backoff=backoff_handler)
    def get_trending_movies(self, limit=1000, languages=None):
        try:
            processed_movies = []

            if languages is None:
                languages = ['en']

            # generate payload
            payload = {'extended': 'full', 'limit': limit, 'page': 1}
            if languages:
                payload['languages'] = ','.join(languages)

            # make request
            while True:
                req = requests.get(
                    'https://api.trakt.tv/movies/trending',
                    headers=self.headers,
                    params=payload,
                    timeout=30
                )
                log.debug("Request URL: %s", req.url)
                log.debug("Request Payload: %s", payload)
                log.debug("Response Code: %d", req.status_code)
                log.debug("Response Page: %d of %d", payload['page'],
                          0 if 'X-Pagination-Page-Count' not in req.headers else int(
                              req.headers['X-Pagination-Page-Count']))

                if req.status_code == 200:
                    resp_json = req.json()

                    for movie in resp_json:
                        if movie not in processed_movies:
                            processed_movies.append(movie)

                    # check if we have fetched the last page, break if so
                    if 'X-Pagination-Page-Count' not in req.headers or not int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There was no more pages to retrieve")
                        break
                    elif payload['page'] >= int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There are no more pages to retrieve results from")
                        break
                    else:
                        log.info("There are %d pages left to retrieve results from",
                                 int(req.headers['X-Pagination-Page-Count']) - payload['page'])
                        payload['page'] += 1
                        time.sleep(5)

                else:
                    log.error("Failed to retrieve trending movies, request response: %d", req.status_code)
                    break

            if len(processed_movies):
                log.debug("Found %d trending movies", len(processed_movies))
                return processed_movies
            return None
        except Exception:
            log.exception("Exception retrieving trending movies: ")
        return None

    @backoff.on_predicate(backoff.expo, lambda x: x is None, max_tries=4, on_backoff=backoff_handler)
    def get_popular_movies(self, limit=1000, languages=None):
        try:
            processed_movies = []

            if languages is None:
                languages = ['en']

            # generate payload
            payload = {'extended': 'full', 'limit': limit, 'page': 1}
            if languages:
                payload['languages'] = ','.join(languages)

            # make request
            while True:
                req = requests.get(
                    'https://api.trakt.tv/movies/popular',
                    headers=self.headers,
                    params=payload,
                    timeout=30
                )
                log.debug("Request URL: %s", req.url)
                log.debug("Request Payload: %s", payload)
                log.debug("Response Code: %d", req.status_code)
                log.debug("Response Page: %d of %d", payload['page'],
                          0 if 'X-Pagination-Page-Count' not in req.headers else int(
                              req.headers['X-Pagination-Page-Count']))

                if req.status_code == 200:
                    resp_json = req.json()

                    # process list so it conforms to standard we expect ( e.g. {"movie": {.....}} )
                    for movie in resp_json:
                        if movie not in processed_movies:
                            processed_movies.append({'movie': movie})

                    # check if we have fetched the last page, break if so
                    if 'X-Pagination-Page-Count' not in req.headers or not int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There was no more pages to retrieve")
                        break
                    elif payload['page'] >= int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There are no more pages to retrieve results from")
                        break
                    else:
                        log.info("There are %d pages left to retrieve results from",
                                 int(req.headers['X-Pagination-Page-Count']) - payload['page'])
                        payload['page'] += 1
                        time.sleep(5)

                else:
                    log.error("Failed to retrieve popular movies, request response: %d", req.status_code)
                    break

            if len(processed_movies):
                log.debug("Found %d popular movies", len(processed_movies))
                return processed_movies
            return None
        except Exception:
            log.exception("Exception retrieving popular movies: ")
        return None

    @backoff.on_predicate(backoff.expo, lambda x: x is None, max_tries=4, on_backoff=backoff_handler)
    def get_boxoffice_movies(self, limit=1000, languages=None):
        try:
            processed_movies = []

            if languages is None:
                languages = ['en']

            # generate payload
            payload = {'extended': 'full', 'limit': limit, 'page': 1}
            if languages:
                payload['languages'] = ','.join(languages)

            # make request
            while True:
                req = requests.get(
                    'https://api.trakt.tv/movies/boxoffice',
                    headers=self.headers,
                    params=payload,
                    timeout=30
                )
                log.debug("Request URL: %s", req.url)
                log.debug("Request Payload: %s", payload)
                log.debug("Response Code: %d", req.status_code)
                log.debug("Response Page: %d of %d", payload['page'],
                          0 if 'X-Pagination-Page-Count' not in req.headers else int(
                              req.headers['X-Pagination-Page-Count']))

                if req.status_code == 200:
                    resp_json = req.json()

                    for movie in resp_json:
                        if movie not in processed_movies:
                            processed_movies.append(movie)

                    # check if we have fetched the last page, break if so
                    if 'X-Pagination-Page-Count' not in req.headers or not int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There was no more pages to retrieve")
                        break
                    elif payload['page'] >= int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There are no more pages to retrieve results from")
                        break
                    else:
                        log.info("There are %d pages left to retrieve results from",
                                 int(req.headers['X-Pagination-Page-Count']) - payload['page'])
                        payload['page'] += 1
                        time.sleep(5)

                else:
                    log.error("Failed to retrieve boxoffice movies, request response: %d", req.status_code)
                    break

            if len(processed_movies):
                log.debug("Found %d boxoffice movies", len(processed_movies))
                return processed_movies
            return None
        except Exception:
            log.exception("Exception retrieving boxoffice movies: ")
        return None

    @backoff.on_predicate(backoff.expo, lambda x: x is None, max_tries=4, on_backoff=backoff_handler)
    def get_watchlist_movies(self, user=None, limit=1000, languages=None):
        try:
            processed_movies = []

            if languages is None:
                languages = ['en']

            # generate payload
            payload = {'extended': 'full', 'limit': limit, 'page': 1}
            if languages:
                payload['languages'] = ','.join(languages)

            # make request
            while True:
                req = requests.get('https://api.trakt.tv/sync/watchlist/movies', params=payload,
                                   headers=self.oauth_headers(user),
                                   timeout=30)
                log.debug("Request User: %s", user)
                log.debug("Request URL: %s", req.url)
                log.debug("Request Payload: %s", payload)
                log.debug("Response Code: %d", req.status_code)
                log.debug("Response Page: %d of %d", payload['page'],
                          0 if 'X-Pagination-Page-Count' not in req.headers else int(
                              req.headers['X-Pagination-Page-Count']))

                if req.status_code == 200:
                    resp_json = req.json()

                    for show in resp_json:
                        if show not in processed_movies:
                            processed_movies.append(show)

                    # check if we have fetched the last page, break if so
                    if 'X-Pagination-Page-Count' not in req.headers or not int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There was no more pages to retrieve")
                        break
                    elif payload['page'] >= int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There are no more pages to retrieve results from")
                        break
                    else:
                        log.info("There are %d pages left to retrieve results from",
                                 int(req.headers['X-Pagination-Page-Count']) - payload['page'])
                        payload['page'] += 1
                        time.sleep(5)
                elif req.status_code == 401:
                    log.error("The authentication to Trakt is revoked. Please re-authenticate.")

                    exit()
                else:
                    log.error("Failed to retrieve movies on watchlist from %s, request response: %d", user,
                              req.status_code)
                    break

            if len(processed_movies):
                log.debug("Found %d movies on watchlist from %s", len(processed_movies), user)

                return processed_movies
            return None
        except Exception:
            log.exception("Exception retrieving movies on watchlist")
        return None

    @backoff.on_predicate(backoff.expo, lambda x: x is None, max_tries=4, on_backoff=backoff_handler)
    def get_user_list_movies(self, list_id, user=None, limit=1000, languages=None):
        try:
            processed_movies = []

            if languages is None:
                languages = ['en']

            # generate payload
            payload = {'extended': 'full', 'limit': limit, 'page': 1}
            if languages:
                payload['languages'] = ','.join(languages)

            # make request
            while True:
                req = requests.get('https://api.trakt.tv/users/' + user + '/lists/' + list_id + '/items/movies',
                                   params=payload,
                                   headers=self.oauth_headers(user),
                                   timeout=30)
                log.debug("Request User: %s", user)
                log.debug("Request URL: %s", req.url)
                log.debug("Request Payload: %s", payload)
                log.debug("Response Code: %d", req.status_code)
                log.debug("Response Page: %d of %d", payload['page'],
                          0 if 'X-Pagination-Page-Count' not in req.headers else int(
                              req.headers['X-Pagination-Page-Count']))

                if req.status_code == 200:
                    resp_json = req.json()

                    for show in resp_json:
                        if show not in processed_movies:
                            processed_movies.append(show)

                    # check if we have fetched the last page, break if so
                    if 'X-Pagination-Page-Count' not in req.headers or not int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There was no more pages to retrieve")
                        break
                    elif payload['page'] >= int(req.headers['X-Pagination-Page-Count']):
                        log.debug("There are no more pages to retrieve results from")
                        break
                    else:
                        log.info("There are %d pages left to retrieve results from",
                                 int(req.headers['X-Pagination-Page-Count']) - payload['page'])
                        payload['page'] += 1
                        time.sleep(5)
                elif req.status_code == 401:
                    log.error("The authentication to Trakt is revoked. Please re-authenticate.")

                    exit()
                else:
                    log.error("Failed to retrieve movies on watchlist from %s, request response: %d", user,
                              req.status_code)
                    break

            if len(processed_movies):
                log.debug("Found %d movies on watchlist from %s", len(processed_movies), user)

                return processed_movies
            return None
        except Exception:
            log.exception("Exception retrieving movies on watchlist")
        return None
