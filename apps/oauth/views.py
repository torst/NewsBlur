import urllib
import urlparse
import datetime
import lxml.html
from django.contrib.auth.decorators import login_required
from django.core.urlresolvers import reverse
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.http import HttpResponseForbidden, HttpResponseRedirect
from django.conf import settings
from mongoengine.queryset import OperationError
from apps.social.models import MSocialServices, MSocialSubscription, MSharedStory
from apps.social.tasks import SyncTwitterFriends, SyncFacebookFriends, SyncAppdotnetFriends
from apps.reader.models import UserSubscription, UserSubscriptionFolders, RUserStory
from apps.analyzer.models import MClassifierTitle, MClassifierAuthor, MClassifierFeed, MClassifierTag
from apps.analyzer.models import compute_story_score
from apps.rss_feeds.models import Feed, MStory, MStarredStoryCounts, MStarredStory
from utils import log as logging
from utils.user_functions import ajax_login_required
from utils.view_functions import render_to
from utils import json_functions as json
from vendor import facebook
from vendor import tweepy
from vendor import appdotnet

@login_required
@render_to('social/social_connect.xhtml')
def twitter_connect(request):
    twitter_consumer_key = settings.TWITTER_CONSUMER_KEY
    twitter_consumer_secret = settings.TWITTER_CONSUMER_SECRET
    
    oauth_token = request.REQUEST.get('oauth_token')
    oauth_verifier = request.REQUEST.get('oauth_verifier')
    denied = request.REQUEST.get('denied')
    if denied:
        logging.user(request, "~BB~FRDenied Twitter connect")
        return {'error': 'Denied! Try connecting again.'}
    elif oauth_token and oauth_verifier:
        try:
            auth = tweepy.OAuthHandler(twitter_consumer_key, twitter_consumer_secret)
            auth.set_request_token(oauth_token, oauth_verifier)
            access_token = auth.get_access_token(oauth_verifier)
            api = tweepy.API(auth)
            twitter_user = api.me()
        except (tweepy.TweepError, IOError):
            logging.user(request, "~BB~FRFailed Twitter connect")
            return dict(error="Twitter has returned an error. Try connecting again.")

        # Be sure that two people aren't using the same Twitter account.
        existing_user = MSocialServices.objects.filter(twitter_uid=unicode(twitter_user.id))
        if existing_user and existing_user[0].user_id != request.user.pk:
            try:
                user = User.objects.get(pk=existing_user[0].user_id)
                logging.user(request, "~BB~FRFailed Twitter connect, another user: %s" % user.username)
                return dict(error=("Another user (%s, %s) has "
                                   "already connected with those Twitter credentials."
                                   % (user.username, user.email or "no email")))
            except User.DoesNotExist:
                existing_user.delete()

        social_services, _ = MSocialServices.objects.get_or_create(user_id=request.user.pk)
        social_services.twitter_uid = unicode(twitter_user.id)
        social_services.twitter_access_key = access_token.key
        social_services.twitter_access_secret = access_token.secret
        social_services.syncing_twitter = True
        social_services.save()

        SyncTwitterFriends.delay(user_id=request.user.pk)
        
        logging.user(request, "~BB~FRFinishing Twitter connect")
        return {}
    else:
        # Start the OAuth process
        auth = tweepy.OAuthHandler(twitter_consumer_key, twitter_consumer_secret)
        auth_url = auth.get_authorization_url()
        logging.user(request, "~BB~FRStarting Twitter connect")
        return {'next': auth_url}


@login_required
@render_to('social/social_connect.xhtml')
def facebook_connect(request):
    facebook_app_id = settings.FACEBOOK_APP_ID
    facebook_secret = settings.FACEBOOK_SECRET
    
    args = {
        "client_id": facebook_app_id,
        "redirect_uri": "http://" + Site.objects.get_current().domain + reverse('facebook-connect'),
        "scope": "offline_access,user_website,publish_actions",
        "display": "popup",
    }

    verification_code = request.REQUEST.get('code')
    if verification_code:
        args["client_secret"] = facebook_secret
        args["code"] = verification_code
        uri = "https://graph.facebook.com/oauth/access_token?" + \
                urllib.urlencode(args)
        response_text = urllib.urlopen(uri).read()
        response = urlparse.parse_qs(response_text)

        if "access_token" not in response:
            logging.user(request, "~BB~FRFailed Facebook connect")
            return dict(error="Facebook has returned an error. Try connecting again.")

        access_token = response["access_token"][-1]

        # Get the user's profile.
        graph = facebook.GraphAPI(access_token)
        profile = graph.get_object("me")
        uid = profile["id"]

        # Be sure that two people aren't using the same Facebook account.
        existing_user = MSocialServices.objects.filter(facebook_uid=uid)
        if existing_user and existing_user[0].user_id != request.user.pk:
            try:
                user = User.objects.get(pk=existing_user[0].user_id)
                logging.user(request, "~BB~FRFailed FB connect, another user: %s" % user.username)
                return dict(error=("Another user (%s, %s) has "
                                   "already connected with those Facebook credentials."
                                   % (user.username, user.email or "no email")))
            except User.DoesNotExist:
                existing_user.delete()

        social_services, _ = MSocialServices.objects.get_or_create(user_id=request.user.pk)
        social_services.facebook_uid = uid
        social_services.facebook_access_token = access_token
        social_services.syncing_facebook = True
        social_services.save()
        
        SyncFacebookFriends.delay(user_id=request.user.pk)
        
        logging.user(request, "~BB~FRFinishing Facebook connect")
        return {}
    elif request.REQUEST.get('error'):
        logging.user(request, "~BB~FRFailed Facebook connect")
        return {'error': '%s... Try connecting again.' % request.REQUEST.get('error')}
    else:
        # Start the OAuth process
        logging.user(request, "~BB~FRStarting Facebook connect")
        url = "https://www.facebook.com/dialog/oauth?" + urllib.urlencode(args)
        return {'next': url}

@login_required
@render_to('social/social_connect.xhtml')
def appdotnet_connect(request):
    domain = Site.objects.get_current().domain
    args = {
        "client_id": settings.APPDOTNET_CLIENTID,
        "client_secret": settings.APPDOTNET_SECRET,
        "redirect_uri": "http://" + domain +
                                    reverse('appdotnet-connect'),
        "scope": ["email", "write_post", "follow"],
    }

    oauth_code = request.REQUEST.get('code')
    denied = request.REQUEST.get('denied')
    if denied:
        logging.user(request, "~BB~FRDenied App.net connect")
        return {'error': 'Denied! Try connecting again.'}
    elif oauth_code:
        try:
            adn_auth = appdotnet.Appdotnet(**args)
            response = adn_auth.getAuthResponse(oauth_code)
            adn_resp = json.decode(response)
            access_token = adn_resp['access_token']
            adn_userid = adn_resp['user_id']
        except (IOError):
            logging.user(request, "~BB~FRFailed App.net connect")
            return dict(error="App.net has returned an error. Try connecting again.")

        # Be sure that two people aren't using the same Twitter account.
        existing_user = MSocialServices.objects.filter(appdotnet_uid=unicode(adn_userid))
        if existing_user and existing_user[0].user_id != request.user.pk:
            try:
                user = User.objects.get(pk=existing_user[0].user_id)
                logging.user(request, "~BB~FRFailed App.net connect, another user: %s" % user.username)
                return dict(error=("Another user (%s, %s) has "
                                   "already connected with those App.net credentials."
                                   % (user.username, user.email or "no email")))
            except User.DoesNotExist:
                existing_user.delete()
        
        social_services, _ = MSocialServices.objects.get_or_create(user_id=request.user.pk)
        social_services.appdotnet_uid = unicode(adn_userid)
        social_services.appdotnet_access_token = access_token
        social_services.syncing_appdotnet = True
        social_services.save()
        
        SyncAppdotnetFriends.delay(user_id=request.user.pk)
        
        logging.user(request, "~BB~FRFinishing App.net connect")
        return {}
    else:
        # Start the OAuth process
        adn_auth = appdotnet.Appdotnet(**args)
        auth_url = adn_auth.generateAuthUrl()
        logging.user(request, "~BB~FRStarting App.net connect")
        return {'next': auth_url}

@ajax_login_required
def twitter_disconnect(request):
    logging.user(request, "~BB~FRDisconnecting Twitter")
    social_services = MSocialServices.objects.get(user_id=request.user.pk)
    social_services.disconnect_twitter()
    
    return HttpResponseRedirect(reverse('load-user-friends'))

@ajax_login_required
def facebook_disconnect(request):
    logging.user(request, "~BB~FRDisconnecting Facebook")
    social_services = MSocialServices.objects.get(user_id=request.user.pk)
    social_services.disconnect_facebook()
    
    return HttpResponseRedirect(reverse('load-user-friends'))
    
@ajax_login_required
def appdotnet_disconnect(request):
    logging.user(request, "~BB~FRDisconnecting App.net")
    social_services = MSocialServices.objects.get(user_id=request.user.pk)
    social_services.disconnect_appdotnet()
    
    return HttpResponseRedirect(reverse('load-user-friends'))
    
@ajax_login_required
@json.json_view
def follow_twitter_account(request):
    username = request.POST['username']
    code = 1
    message = "OK"
    
    logging.user(request, "~BB~FR~SKFollowing Twitter: %s" % username)
    
    if username not in ['samuelclay', 'newsblur']:
        return HttpResponseForbidden()
    
    social_services = MSocialServices.objects.get(user_id=request.user.pk)
    try:
        api = social_services.twitter_api()
        api.create_friendship(username)
    except tweepy.TweepError, e:
        code = -1
        message = e
        
    return {'code': code, 'message': message}
    
@ajax_login_required
@json.json_view
def unfollow_twitter_account(request):
    username = request.POST['username']
    code = 1
    message = "OK"
    
    logging.user(request, "~BB~FRUnfollowing Twitter: %s" % username)
        
    if username not in ['samuelclay', 'newsblur']:
        return HttpResponseForbidden()
    
    social_services = MSocialServices.objects.get(user_id=request.user.pk)
    try:
        api = social_services.twitter_api()
        api.destroy_friendship(username)
    except tweepy.TweepError, e:
        code = -1
        message = e
    
    return {'code': code, 'message': message}

@login_required
@json.json_view
def api_user_info(request):
    user = request.user
    
    return {"data": {
        "name": user.username,
        "id": user.pk,
    }}
    
@login_required
@json.json_view
def api_feed_list(request, trigger_slug=None):
    user = request.user
    usf = UserSubscriptionFolders.objects.get(user=user)
    flat_folders = usf.flatten_folders()
    titles = [dict(label=" - Folder: All Site Stories", value="all")]
    feeds = {}
    
    user_subs = UserSubscription.objects.select_related('feed').filter(user=user, active=True)    
    
    for sub in user_subs:
        feeds[sub.feed_id] = sub.canonical()
    
    for folder_title in sorted(flat_folders.keys()):
        if folder_title and folder_title != " ":
            titles.append(dict(label=" - Folder: %s" % folder_title, value=folder_title, optgroup=True))
        else:
            titles.append(dict(label=" - Folder: Top Level", value="Top Level", optgroup=True))
        folder_contents = []
        for feed_id in flat_folders[folder_title]:
            if feed_id not in feeds: continue
            feed = feeds[feed_id]
            folder_contents.append(dict(label=feed['feed_title'], value=str(feed['id'])))
        folder_contents = sorted(folder_contents, key=lambda f: f['label'].lower())
        titles.extend(folder_contents)
        
    return {"data": titles}
    
@login_required
@json.json_view
def api_folder_list(request, trigger_slug=None):
    user = request.user
    usf = UserSubscriptionFolders.objects.get(user=user)
    flat_folders = usf.flatten_folders()
    titles = [dict(label="All Site Stories", value="all")]
    
    for folder_title in sorted(flat_folders.keys()):
        if folder_title and folder_title != " ":
            titles.append(dict(label=folder_title, value=folder_title))
        else:
            titles.append(dict(label="Top Level", value="Top Level"))
        
    return {"data": titles}

@login_required
@json.json_view
def api_saved_tag_list(request):
    user = request.user
    starred_counts, starred_count = MStarredStoryCounts.user_counts(user.pk, include_total=True)
    tags = []
    
    for tag in starred_counts:
        if tag['tag'] == "": continue
        tags.append(dict(label="%s (%s %s)" % (tag['tag'], tag['count'], 
                                               'story' if tag['count'] == 1 else 'stories'),
                         value=tag['tag']))
    tags = sorted(tags, key=lambda t: t['value'].lower())
    catchall = dict(label="All Saved Stories (%s %s)" % (starred_count,
                                                         'story' if starred_count == 1 else 'stories'),
                    value="all")
    tags.insert(0, catchall)
    
    return {"data": tags}

@login_required
@json.json_view
def api_shared_usernames(request):
    user = request.user
    social_feeds = MSocialSubscription.feeds(user_id=user.pk)
    blurblogs = []

    for social_feed in social_feeds:
        if not social_feed['shared_stories_count']: continue
        blurblogs.append(dict(label="%s (%s %s)" % (social_feed['username'],
                                                    social_feed['shared_stories_count'], 
                                                    'story' if social_feed['shared_stories_count'] == 1 else 'stories'),
                         value=social_feed['user_id']))
    blurblogs = sorted(blurblogs, key=lambda b: b['label'].lower())
    catchall = dict(label="All Shared Stories",
                    value="all")
    blurblogs.insert(0, catchall)
    
    return {"data": blurblogs}

@login_required
@json.json_view
def api_unread_story(request, unread_score=None):
    user = request.user
    body = json.decode(request.body)
    after = body.get('after', None)
    before = body.get('before', None)
    limit = body.get('limit', 50)
    fields = body.get('triggerFields')
    feed_or_folder = fields['feed_or_folder']
    entries = []

    if feed_or_folder.isdigit():
        feed_id = int(feed_or_folder)
        usersub = UserSubscription.objects.get(user=user, feed_id=feed_id)
        found_feed_ids = [feed_id]
        found_trained_feed_ids = [feed_id] if usersub.is_trained else []
        stories = usersub.get_stories(order="newest", read_filter="unread", 
                                      offset=0, limit=limit,
                                      default_cutoff_date=user.profile.unread_cutoff)
    else:
        folder_title = feed_or_folder
        if folder_title == "Top Level":
            folder_title = " "
        usf = UserSubscriptionFolders.objects.get(user=user)
        flat_folders = usf.flatten_folders()
        feed_ids = None
        if folder_title != "all":
            feed_ids = flat_folders.get(folder_title)
        usersubs = UserSubscription.subs_for_feeds(user.pk, feed_ids=feed_ids,
                                                   read_filter="unread")
        feed_ids = [sub.feed_id for sub in usersubs]
        params = {
            "user_id": user.pk, 
            "feed_ids": feed_ids,
            "offset": 0,
            "limit": limit,
            "order": "newest",
            "read_filter": "unread",
            "usersubs": usersubs,
            "cutoff_date": user.profile.unread_cutoff,
        }
        story_hashes, unread_feed_story_hashes = UserSubscription.feed_stories(**params)
        mstories = MStory.objects(story_hash__in=story_hashes).order_by('-story_date')
        stories = Feed.format_stories(mstories)
        found_feed_ids = list(set([story['story_feed_id'] for story in stories]))
        trained_feed_ids = [sub.feed_id for sub in usersubs if sub.is_trained]
        found_trained_feed_ids = list(set(trained_feed_ids) & set(found_feed_ids))
    
    if found_trained_feed_ids:
        classifier_feeds = list(MClassifierFeed.objects(user_id=user.pk,
                                                        feed_id__in=found_trained_feed_ids))
        classifier_authors = list(MClassifierAuthor.objects(user_id=user.pk, 
                                                            feed_id__in=found_trained_feed_ids))
        classifier_titles = list(MClassifierTitle.objects(user_id=user.pk, 
                                                          feed_id__in=found_trained_feed_ids))
        classifier_tags = list(MClassifierTag.objects(user_id=user.pk, 
                                                      feed_id__in=found_trained_feed_ids))
    feeds = dict([(f.pk, {
        "title": f.feed_title,
        "website": f.feed_link,
        "address": f.feed_address,
    }) for f in Feed.objects.filter(pk__in=found_feed_ids)])

    for story in stories:
        if before and int(story['story_date'].strftime("%s")) > before: continue
        if after and int(story['story_date'].strftime("%s")) < after: continue
        score = 0
        if found_trained_feed_ids and story['story_feed_id'] in found_trained_feed_ids:
            score = compute_story_score(story, classifier_titles=classifier_titles, 
                                        classifier_authors=classifier_authors, 
                                        classifier_tags=classifier_tags,
                                        classifier_feeds=classifier_feeds)
            if score < 0: continue
            if unread_score == "new-focus-story" and score < 1: continue
        feed = feeds.get(story['story_feed_id'], None)
        entries.append({
            "StoryTitle": story['story_title'],
            "StoryContent": story['story_content'],
            "StoryUrl": story['story_permalink'],
            "StoryAuthor": story['story_authors'],
            "StoryDate": story['story_date'].isoformat(),
            "StoryScore": score,
            "SiteTitle": feed and feed['title'],
            "SiteWebsite": feed and feed['website'],
            "SiteFeedAddress": feed and feed['address'],
            "ifttt": {
                "id": story['story_hash'],
                "timestamp": int(story['story_date'].strftime("%s"))
            },
        })
    
    return {"data": entries}

@login_required
@json.json_view
def api_saved_story(request):
    user = request.user
    body = json.decode(request.body)
    after = body.get('after', None)
    before = body.get('before', None)
    limit = body.get('limit', 50)
    fields = body.get('triggerFields')
    story_tag = fields['story_tag']
    entries = []
    
    if story_tag == "all":
        story_tag = ""
    
    mstories = MStarredStory.objects(
        user_id=user.pk,
        user_tags__contains=story_tag
    ).order_by('-starred_date')[:limit]
    stories = Feed.format_stories(mstories)        
    
    found_feed_ids = list(set([story['story_feed_id'] for story in stories]))
    feeds = dict([(f.pk, {
        "title": f.feed_title,
        "website": f.feed_link,
        "address": f.feed_address,
    }) for f in Feed.objects.filter(pk__in=found_feed_ids)])

    for story in stories:
        if before and int(story['story_date'].strftime("%s")) > before: continue
        if after and int(story['story_date'].strftime("%s")) < after: continue
        feed = feeds.get(story['story_feed_id'], None)
        entries.append({
            "StoryTitle": story['story_title'],
            "StoryContent": story['story_content'],
            "StoryUrl": story['story_permalink'],
            "StoryAuthor": story['story_authors'],
            "StoryDate": story['story_date'].isoformat(),
            "SavedDate": story['starred_date'].isoformat(),
            "SavedTags": ', '.join(story['user_tags']),
            "SiteTitle": feed and feed['title'],
            "SiteWebsite": feed and feed['website'],
            "SiteFeedAddress": feed and feed['address'],
            "ifttt": {
                "id": story['story_hash'],
                "timestamp": int(story['starred_date'].strftime("%s"))
            },
        })
    
    return {"data": entries}
    
@login_required
@json.json_view
def api_shared_story(request):
    user = request.user
    body = json.decode(request.body)
    after = body.get('after', None)
    before = body.get('before', None)
    limit = body.get('limit', 50)
    fields = body.get('triggerFields')
    blurblog_user = fields['blurblog_user']
    entries = []
    
    if blurblog_user.isdigit():
        social_user_ids = [int(blurblog_user)]
    elif blurblog_user == "all":
        socialsubs = MSocialSubscription.objects.filter(user_id=user.pk)
        social_user_ids = [ss.subscription_user_id for ss in socialsubs]

    mstories = MSharedStory.objects(
        user_id__in=social_user_ids
    ).order_by('-shared_date')[:limit]        
    stories = Feed.format_stories(mstories)
    
    found_feed_ids = list(set([story['story_feed_id'] for story in stories]))
    share_user_ids = list(set([story['user_id'] for story in stories]))
    users = dict([(u.pk, u.username) 
                 for u in User.objects.filter(pk__in=share_user_ids).only('pk', 'username')])
    feeds = dict([(f.pk, {
        "title": f.feed_title,
        "website": f.feed_link,
        "address": f.feed_address,
    }) for f in Feed.objects.filter(pk__in=found_feed_ids)])
    
    classifier_feeds   = list(MClassifierFeed.objects(user_id=user.pk, 
                                                      social_user_id__in=social_user_ids))
    classifier_authors = list(MClassifierAuthor.objects(user_id=user.pk,
                                                        social_user_id__in=social_user_ids))
    classifier_titles  = list(MClassifierTitle.objects(user_id=user.pk,
                                                       social_user_id__in=social_user_ids))
    classifier_tags    = list(MClassifierTag.objects(user_id=user.pk, 
                                                     social_user_id__in=social_user_ids))
    # Merge with feed specific classifiers
    classifier_feeds   = classifier_feeds + list(MClassifierFeed.objects(user_id=user.pk,
                                                                         feed_id__in=found_feed_ids))
    classifier_authors = classifier_authors + list(MClassifierAuthor.objects(user_id=user.pk,
                                                                             feed_id__in=found_feed_ids))
    classifier_titles  = classifier_titles + list(MClassifierTitle.objects(user_id=user.pk,
                                                                           feed_id__in=found_feed_ids))
    classifier_tags    = classifier_tags + list(MClassifierTag.objects(user_id=user.pk,
                                                                       feed_id__in=found_feed_ids))
        
    for story in stories:
        if before and int(story['shared_date'].strftime("%s")) > before: continue
        if after and int(story['shared_date'].strftime("%s")) < after: continue
        score = compute_story_score(story, classifier_titles=classifier_titles, 
                                    classifier_authors=classifier_authors, 
                                    classifier_tags=classifier_tags,
                                    classifier_feeds=classifier_feeds)
        if score < 0: continue
        feed = feeds.get(story['story_feed_id'], None)
        entries.append({
            "StoryTitle": story['story_title'],
            "StoryContent": story['story_content'],
            "StoryUrl": story['story_permalink'],
            "StoryAuthor": story['story_authors'],
            "StoryDate": story['story_date'].isoformat(),
            "StoryScore": score,
            "SharedComments": story['comments'],
            "ShareUsername": users.get(story['user_id']),
            "SharedDate": story['shared_date'].isoformat(),
            "SiteTitle": feed and feed['title'],
            "SiteWebsite": feed and feed['website'],
            "SiteFeedAddress": feed and feed['address'],
            "ifttt": {
                "id": story['story_hash'],
                "timestamp": int(story['shared_date'].strftime("%s"))
            },
        })

    return {"data": entries}

@json.json_view
def ifttt_status(request):
    return {"data": {
        "status": "OK",
        "time": datetime.datetime.now().isoformat()
    }}

@login_required
@json.json_view
def api_share_new_story(request):
    user = request.user
    body = json.decode(request.body)
    fields = body.get('actionFields')
    story_url = fields['story_url']
    content = fields.get('story_content', "")
    story_title = fields.get('story_title', "[Untitled]")
    story_author = fields.get('story_author', "")
    comments = fields.get('comments', None)

    feed = Feed.get_feed_from_url(story_url, create=True, fetch=True)
    
    content = lxml.html.fromstring(content)
    content.make_links_absolute(story_url)
    content = lxml.html.tostring(content)
    
    shared_story = MSharedStory.objects.filter(user_id=user.pk,
                                               story_feed_id=feed and feed.pk or 0,
                                               story_guid=story_url).limit(1).first()
    if not shared_story:
        story_db = {
            "story_guid": story_url,
            "story_permalink": story_url,
            "story_title": story_title,
            "story_feed_id": feed and feed.pk or 0,
            "story_content": content,
            "story_author": story_author,
            "story_date": datetime.datetime.now(),
            "user_id": user.pk,
            "comments": comments,
            "has_comments": bool(comments),
        }
        shared_story = MSharedStory.objects.create(**story_db)
        socialsubs = MSocialSubscription.objects.filter(subscription_user_id=user.pk)
        for socialsub in socialsubs:
            socialsub.needs_unread_recalc = True
            socialsub.save()
        logging.user(request, "~BM~FYSharing story from site: ~SB%s: %s" % (story_url, comments))
    else:
        logging.user(request, "~BM~FY~SBAlready~SN shared story from IFTTT: ~SB%s: %s" % (story_url, comments))
    
    try:
        socialsub = MSocialSubscription.objects.get(user_id=user.pk, 
                                                    subscription_user_id=user.pk)
    except MSocialSubscription.DoesNotExist:
        socialsub = None
    
    if socialsub:
        socialsub.mark_story_ids_as_read([shared_story.story_hash], 
                                          shared_story.story_feed_id, 
                                          request=request)
    else:
        RUserStory.mark_read(user.pk, shared_story.story_feed_id, shared_story.story_hash)

    shared_story.publish_update_to_subscribers()
    
    return {"data": [{
        "id": shared_story and shared_story.story_guid,
        "url": shared_story and shared_story.blurblog_permalink()
    }]}

@login_required
@json.json_view
def api_save_new_story(request):
    user = request.user
    body = json.decode(request.body)
    fields = body.get('actionFields')
    story_url = fields['story_url']
    story_content = fields.get('story_content', "")
    story_title = fields.get('story_title', "[Untitled]")
    story_author = fields.get('story_author', "")
    user_tags = fields.get('user_tags', "")
    story = None
    
    try:
        original_feed = Feed.get_feed_from_url(story_url)
        story_db = {
            "user_id": user.pk,
            "starred_date": datetime.datetime.now(),
            "story_date": datetime.datetime.now(),
            "story_title": story_title or '[Untitled]',
            "story_permalink": story_url,
            "story_guid": story_url,
            "story_content": story_content,
            "story_author_name": story_author,
            "story_feed_id": original_feed and original_feed.pk or 0,
            "user_tags": [tag for tag in user_tags.split(',')]
        }
        logging.user(request, "~FCStarring by IFTTT: ~SB%s~SN in ~SB%s" % (story_db['story_title'][:50], original_feed and original_feed))
        story = MStarredStory.objects.create(**story_db)
        MStarredStoryCounts.count_tags_for_user(user.pk)
    except OperationError:
        logging.user(request, "~FCAlready starred: ~SB%s" % (story_db['story_title'][:50]))
        pass
    
    return {"data": [{
        "id": story and story.id,
        "url": story and story.story_permalink
    }]}

@login_required
@json.json_view
def api_save_new_subscription(request):
    user = request.user
    body = json.decode(request.body)
    fields = body.get('actionFields')
    url = fields['url']
    folder = fields['folder']
    
    if folder == "Top Level":
        folder = " "
    
    code, message, us = UserSubscription.add_subscription(
        user=user, 
        feed_address=url,
        folder=folder,
        bookmarklet=True
    )
    
    logging.user(request, "~FRAdding URL from IFTTT: ~SB%s (in %s)" % (url, folder))

    if us and us.feed:
        url = us.feed.feed_address

    return {"data": [{
        "id": us and us.feed_id,
        "url": url,
    }]}
