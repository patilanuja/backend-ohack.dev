from common.utils import safe_get_env_var
from common.utils.slack import send_slack_audit, create_slack_channel, send_slack, invite_user_to_channel
from common.utils.firebase import get_hackathon_by_event_id, upsert_news
from common.utils.openai_api import generate_and_save_image_to_cdn
from common.utils.github import create_github_repo
from api.messages.message import Message
from services.users_service import get_propel_user_details_by_id, get_slack_user_from_propel_user_id, save_user
import json
import uuid
from datetime import datetime, timedelta
import time

import logging
import firebase_admin
from firebase_admin import credentials, firestore
import requests

from cachetools import cached, LRUCache, TTLCache
from cachetools.keys import hashkey

from ratelimit import limits
from datetime import datetime, timedelta
import os

from db.db import fetch_user_by_user_id, get_user_doc_reference



logger = logging.getLogger("myapp")
logger.setLevel(logging.INFO)


google_recaptcha_key = safe_get_env_var("GOOGLE_CAPTCHA_SECRET_KEY")

CDN_SERVER = os.getenv("CDN_SERVER")
ONE_MINUTE = 1*60
THIRTY_SECONDS = 30
def get_public_message():
    logger.debug("~ Public ~")
    return Message(
        "aaThis is a public message."
    )


def get_protected_message():
    logger.debug("~ Protected ~")

    return Message(
        "This is a protected message."
    )


def get_admin_message():
    logger.debug("~ Admin ~")

    return Message(
        "This is an admin message."
    )

def hash_key(docid, doc=None, depth=0):
    return hashkey(docid)



# Generically handle a DocumentSnapshot or a DocumentReference
#@cached(cache=TTLCache(maxsize=1000, ttl=43200), key=hash_key)
@cached(cache=LRUCache(maxsize=640*1024), key=hash_key)
def doc_to_json(docid=None, doc=None, depth=0):
    # Log
    logger.debug(f"doc_to_json start docid={docid} doc={doc}")
        
    if not docid:
        logger.debug("docid is NoneType")
        return
    if not doc:
        logger.debug("doc is NoneType")
        return
        
    # Check if type is DocumentSnapshot
    if isinstance(doc, firestore.DocumentSnapshot):
        logger.debug("doc is DocumentSnapshot")
        d_json = doc.to_dict()
    # Check if type is DocumentReference
    elif isinstance(doc, firestore.DocumentReference):
        logger.debug("doc is DocumentReference")
        d = doc.get()
        d_json = d.to_dict()    
    else:        
        return doc
    
    if d_json is None:
        logger.warn(f"doc.to_dict() is NoneType | docid={docid} doc={doc}")
        return

    # If any values in d_json is a list, add only the document id to the list for DocumentReference or DocumentSnapshot
    for key, value in d_json.items():
        if isinstance(value, list):
            logger.debug(f"doc_to_json - key={key} value={value}")
            for i, v in enumerate(value):
                logger.debug(f"doc_to_json - i={i} v={v}")
                if isinstance(v, firestore.DocumentReference):
                    logger.debug(f"doc_to_json - v is DocumentReference")
                    value[i] = v.id
                elif isinstance(v, firestore.DocumentSnapshot):
                    logger.debug(f"doc_to_json - v is DocumentSnapshot")
                    value[i] = v.id
                else:
                    logger.debug(f"doc_to_json - v is not DocumentReference or DocumentSnapshot")
                    value[i] = v
            d_json[key] = value
    
            
    
    d_json["id"] = docid
    return d_json

from firebase_admin.firestore import DocumentReference, DocumentSnapshot


# handle DocumentReference or DocumentSnapshot and recursefuly call doc_to_json
def doc_to_json_recursive(doc=None):
    # Log
    logger.debug(f"doc_to_json_recursive start doc={doc}")          
    
    if not doc:
        logger.debug("doc is NoneType")
        return
        
    docid = ""
    # Check if type is DocumentSnapshot
    if isinstance(doc, DocumentSnapshot):
        logger.debug("doc is DocumentSnapshot")
        d_json = doc_to_json(docid=doc.id, doc=doc)
        docid = doc.id
    # Check if type is DocumentReference
    elif isinstance(doc, DocumentReference):
        logger.debug("doc is DocumentReference")
        d = doc.get()
        docid = d.id
        d_json = doc_to_json(docid=doc.id, doc=d)               
    else:
        logger.debug(f"Not DocumentSnapshot or DocumentReference, skipping - returning: {doc}")
        return doc
    
    d_json["id"] = docid
    return d_json


def get_db():
    #mock_db = MockFirestore()
    return firestore.client()

@cached(cache=TTLCache(maxsize=100, ttl=600))
@limits(calls=2000, period=ONE_MINUTE)
def get_single_hackathon_id(id):
    logger.debug(f"get_single_hackathon_id start id={id}")    
    db = get_db()      
    doc = db.collection('hackathons').document(id)
    
    if doc is None:
        logger.warning("get_single_hackathon_id end (no results)")
        return {}
    else:                                
        result = doc_to_json(docid=doc.id, doc=doc)
        result["id"] = doc.id
        
        logger.info(f"get_single_hackathon_id end (with result):{result}")
        return result
    return {}

@cached(cache=TTLCache(maxsize=100, ttl=600))
@limits(calls=2000, period=ONE_MINUTE)
def get_single_hackathon_event(hackathon_id):
    logger.debug(f"get_single_hackathon_event start hackathon_id={hackathon_id}")    
    result = get_hackathon_by_event_id(hackathon_id)
    
    if result is None:
        logger.warning("get_single_hackathon_event end (no results)")
        return {}
    else:                  
        if "nonprofits" in result:           
            result["nonprofits"] = [doc_to_json(doc=npo, docid=npo.id) for npo in result["nonprofits"]]   
        else:
            result["nonprofits"] = []
        if "teams" in result:
            result["teams"] = [doc_to_json(doc=team, docid=team.id) for team in result["teams"]]        
        else:
            result["teams"] = []

        logger.info(f"get_single_hackathon_event end (with result):{result}")
        return result
    return {}

# 12 hour cache for 100 objects LRU
@limits(calls=1000, period=ONE_MINUTE)
def get_single_npo(npo_id):    
    logger.debug(f"get_npo start npo_id={npo_id}")    
    db = get_db()      
    doc = db.collection('nonprofits').document(npo_id)    
    
    if doc is None:
        logger.warning("get_npo end (no results)")
        return {}
    else:                        
        result = doc_to_json(docid=doc.id, doc=doc)

        logger.info(f"get_npo end (with result):{result}")
        return {
            "nonprofits": result
        }
    return {}


@limits(calls=200, period=ONE_MINUTE)
def get_hackathon_list(is_current_only=None):
    logger.debug("Hackathon List Start")
    db = get_db()
    
    if is_current_only == "current":                
        today = datetime.now()        
        today_str = today.strftime("%Y-%m-%d")
        logger.debug(
            f"Looking for any event that finishes after today {today_str} for most current events only.")
        docs = db.collection('hackathons').where("end_date", ">=", today_str).order_by("end_date", direction=firestore.Query.DESCENDING).stream()  # steam() gets all records
    elif is_current_only == "previous": 
        today = datetime.now()
        today_str = today.strftime("%Y-%m-%d")

        N_DAYS_LOOK_BACKWARD = 12*30*3 # 3 years
        target_date = datetime.now() + timedelta(days=-N_DAYS_LOOK_BACKWARD)
        target_date_str = target_date.strftime("%Y-%m-%d")
        logger.debug(
            f"Looking for any event that finishes before today {target_date_str} for previous events only.")
        docs = db.collection('hackathons').where("end_date", ">=", target_date_str).where("end_date", "<=", today_str).order_by("end_date", direction=firestore.Query.DESCENDING).limit(3).stream()  # steam() gets all records       
    else:
        docs = db.collection('hackathons').order_by("start_date").stream()  # steam() gets all records
    
    
    if docs is None:
        logger.debug("Found no results, returning empty list")
        return {[]}
    else:
        results = []
        for doc in docs:
            d = doc_to_json(doc.id, doc)
            # If any value from the keys is a DocumentReference or DocumentSnapshot, call doc_to_json
            for key in d.keys():
                logger.debug(f"Checking key {key}")
                #print type of key
                logger.debug(f"Type of key {type(d[key])}")
                
                # If type is list, iterate through list and call doc_to_json
                if isinstance(d[key], list):
                    logger.debug(f"Found list for key {key}...")
                    # Process all items in list and convert with doc_to_json if they are DocumentReference or DocumentSnapshot
                    for i in range(len(d[key])):
                        logger.debug(f"Processing list item {i}: {d[key][i]}")
                        d[key][i] = doc_to_json_recursive(d[key][i])
                                                                        
                # If type is DocumentReference or DocumentSnapshot, call doc_to_json
                elif isinstance(d[key], DocumentReference) or isinstance(d[key], DocumentSnapshot):
                    logger.debug(f"Found DocumentReference or DocumentSnapshot for key {key}...")
                    d[key] = doc_to_json_recursive(d[key])
                    
                              
            results.append(d)     

    num_results = len(results)
    logger.debug(f"Found {num_results} results")
    logger.debug(f"Results: {results}")
    logger.debug(f"Hackathon List End")
    return {"hackathons": results}


@limits(calls=2000, period=THIRTY_SECONDS)
def get_teams_list(id=None):
    logger.debug(f"Teams List Start team_id={id}")
    db = get_db() 
    if id is not None:
        # Get by id
        doc = db.collection('teams').document(id).get()
        if doc is None:
            return {}
        else:
            #log
            logger.info(f"Teams List team_id={id} | End (with result):{doc_to_json(docid=doc.id, doc=doc)}")
            return doc_to_json(docid=doc.id, doc=doc)
    else:
        # Get all        
        docs = db.collection('teams').stream() # steam() gets all records   
        if docs is None:
            return {[]}
        else:                
            results = []
            for doc in docs:
                results.append(doc_to_json(docid=doc.id, doc=doc))
                                
            return { "teams": results }

@limits(calls=20, period=ONE_MINUTE)
def get_npo_list(word_length=30):
    logger.debug("NPO List Start")
    db = get_db()  
    # steam() gets all records
    docs = db.collection('nonprofits').order_by( "rank" ).stream()
    if docs is None:
        return {[]}
    else:                
        results = []
        for doc in docs:
            logger.debug(f"Processing doc {doc.id} {doc}")
            results.append(doc_to_json_recursive(doc=doc))
           
    # log result
    logger.debug(f"Found {len(results)} results {results}")
    return { "nonprofits": results }

def save_team(propel_user_id, json):    
    send_slack_audit(action="save_team", message="Saving", payload=json)
    slack_user = get_slack_user_from_propel_user_id(propel_user_id)
    slack_user_id = slack_user["sub"]
    
    db = get_db()  # this connects to our Firestore database
    logger.debug("Team Save")    

    logger.debug(json)
    doc_id = uuid.uuid1().hex # Generate a new team id

    name = json["name"]    
    
    root_slack_user_id = slack_user_id.replace("oauth2|slack|T1Q7936BH-","")
    event_id = json["eventId"]
    slack_channel = json["slackChannel"]
    problem_statement_id = json["problemStatementId"]
    github_username = json["githubUsername"]
    
    #TODO: This is not the way
    user = get_user_doc_reference(slack_user_id)
    if user is None:
        return

    problem_statement = get_problem_statement_from_id(problem_statement_id)
    if problem_statement is None:
        return

    # Define vars for github repo creation
    hackathon_event_id = get_single_hackathon_id(event_id)["event_id"]    
    team_name = name
    team_slack_channel = slack_channel
    raw_problem_statement_title = problem_statement.get().to_dict()["title"]
    
    # Remove all spaces from problem_statement_title
    problem_statement_title = raw_problem_statement_title.replace(" ", "").replace("-", "")

    repository_name = f"{team_name}--{problem_statement_title}"
    
    # truncate repostory name to first 100 chars to support github limits
    repository_name = repository_name[:100]

    slack_name_of_creator = user.get().to_dict()["name"]

    project_url = f"https://ohack.dev/project/{problem_statement_id}"
    # Create github repo
    try:
        repo = create_github_repo(repository_name, hackathon_event_id, slack_name_of_creator, team_name, team_slack_channel, problem_statement_id, raw_problem_statement_title, github_username)
    except ValueError as e:
        return {
            "message": f"Error: {e}"
        }
    logger.info(f"Created github repo {repo} for {json}")

    create_slack_channel(slack_channel)
    invite_user_to_channel(slack_user_id, slack_channel)
    
    # Add all Slack admins too  
    slack_admins = ["UC31XTRT5", "UCQKX6LPR", "U035023T81Z", "UC31XTRT5", "UC2JW3T3K", "UPD90QV17", "U05PYC0LMHR"]
    for admin in slack_admins:
        invite_user_to_channel(admin, slack_channel)

    # Send a slack message to the team channel
    slack_message = f'''
:astronaut-floss-dancedance: Team `{name}` | `#{team_slack_channel}` has been created in support of project `{raw_problem_statement_title}` {project_url} by <@{root_slack_user_id}>.

Github repo: {repo['full_url']}
- All code should go here!
- Everything we build is for the public good and carries an MIT license

Questions? join <#C01E5CGDQ74> or use <#C05TVU7HBML> or <#C05TZL13EUD> Slack channels. 
:partyparrot:

Your next steps:
1. Add everyone to your GitHub repo like this: https://opportunity-hack.slack.com/archives/C1Q6YHXQU/p1605657678139600
2. Create your DevPost project https://youtu.be/vCa7QFFthfU?si=bzMQ91d8j3ZkOD03
 - ASU Students use https://opportunity-hack-2023-asu.devpost.com/
 - Everyone else use https://opportunity-hack-2023-virtual.devpost.com/
3. Ask your nonprofit questions and bounce ideas off mentors!
4. Hack the night away!
5. Post any pics to your socials with `#ohack2023` and mention `@opportunityhack`
6. Track any volunteer hours - you are volunteering for a nonprofit!
7. After the hack, update your LinkedIn profile with your new skills and experience!
'''
    send_slack(slack_message, slack_channel)
    send_slack(slack_message, "log-team-creation")

    repo_name = repo["repo_name"]
    full_github_repo_url = repo["full_url"]

    my_date = datetime.now()
    collection = db.collection('teams')    
    insert_res = collection.document(doc_id).set({
        "team_number" : -1,
        "users": [user],        
        "problem_statements": [problem_statement],
        "name": name,        
        "slack_channel": slack_channel,
        "created": my_date.isoformat(),
        "active": "True",
        "github_links": [
            {
                "link": full_github_repo_url,
                "name": repo_name
            }
        ]
    })

    logger.debug(f"Insert Result: {insert_res}")

    # Look up the new team object that was just created
    new_team_doc = db.collection('teams').document(doc_id)
    user_doc = user.get()
    user_dict = user_doc.to_dict()    
    user_teams = user_dict["teams"]
    user_teams.append(new_team_doc)
    user.set({
        "teams": user_teams
    }, merge=True)

    # Get the hackathon (event) - add the team to the event
    event_collection = db.collection("hackathons").document(event_id)
    event_collection_dict = event_collection.get().to_dict()

    new_teams = []     
    for t in event_collection_dict["teams"]:        
        new_teams.append(t)
    new_teams.append(new_team_doc)

    event_collection.set({
        "teams" : new_teams
    }, merge=True)

    # Clear the cache
    logger.info(f"Clearing cache for event_id={event_id} problem_statement_id={problem_statement_id} user_doc.id={user_doc.id} doc_id={doc_id}")
    clear_cache()

    # get the team from get_teams_list
    team = get_teams_list(doc_id)


    return {
        "message" : f"Saved Team and GitHub repo created. See your Slack channel #{slack_channel} for more details.",
        "success" : True,
        "team": team,
        "user": {
            "name" : user_dict["name"],
            "profile_image": user_dict["profile_image"],
        }
        }
        

def join_team(propel_user_id, json):
    send_slack_audit(action="join_team", message="Adding", payload=json)
    db = get_db()  # this connects to our Firestore database
    logger.debug("Join Team Start")

    logger.info(f"Join Team UserId: {propel_user_id} Json: {json}")
    slack_user = get_slack_user_from_propel_user_id(propel_user_id)
    userid = slack_user["sub"]

    team_id = json["teamId"]

    team_doc = db.collection('teams').document(team_id)
    team_dict = team_doc.get().to_dict()

    user_doc = get_user_doc_reference(userid)
    user_dict = user_doc.get().to_dict()
    new_teams = []
    for t in user_dict["teams"]:
        new_teams.append(t)
    new_teams.append(team_doc)
    user_doc.set({
        "teams": new_teams
    }, merge=True)

    new_users = []
    if "users" in team_dict:
        for u in team_dict["users"]:
            new_users.append(u)
    new_users.append(user_doc)  

    # Avoid any duplicate additions
    new_users_set = set(new_users)

    team_doc.set({
        "users": new_users_set
    }, merge=True)    


    # Clear the cache
    logger.info(f"Clearing cache for team_id={team_id} and user_doc.id={user_doc.id}")            
    clear_cache()

    logger.debug("Join Team End")
    return Message("Joined Team")




def unjoin_team(propel_user_id, json):
    send_slack_audit(action="unjoin_team", message="Removing", payload=json)
    db = get_db()  # this connects to our Firestore database
    logger.debug("Unjoin Team Start")
    
    logger.info(f"Unjoin for UserId: {propel_user_id} Json: {json}")
    team_id = json["teamId"]

    slack_user = get_slack_user_from_propel_user_id(propel_user_id)
    userid = slack_user["sub"]

    ## 1. Lookup Team, Remove User 
    doc = db.collection('teams').document(team_id)
    
    if doc:
        doc_dict = doc.get().to_dict()
        send_slack_audit(action="unjoin_team",
                         message="Removing", payload=doc_dict)
        user_list = doc_dict["users"] if "users" in doc_dict else []

        # Look up a team associated with this user and remove that team from their list of teams
        new_users = []
        for u in user_list:
            user_doc = u.get()
            user_dict = user_doc.to_dict()

            new_teams = []
            if userid == user_dict["user_id"]:
                for t in user_dict["teams"]:
                    logger.debug(t.get().id)
                    if t.get().id == team_id:
                        logger.debug("Remove team")                        
                    else:
                        logger.debug("Keep team")
                        new_teams.append(t)
            else:
                logger.debug("Keep user")
                new_users.append(u)
            # Update users collection with new teams
            u.set({
                "teams": new_teams
                }, merge=True) # merging allows to only update this column and not blank everything else out
                    
        doc.set({
            "users": new_users
        }, merge=True)
        logger.debug(new_users)
        
    # Clear the cache
    logger.info(f"Clearing team_id={team_id} cache")         
    clear_cache()
            
    logger.debug("Unjoin Team End")

    return Message(
        "Removed from Team")


@limits(calls=100, period=ONE_MINUTE)
def save_npo(json):    
    send_slack_audit(action="save_npo", message="Saving", payload=json)
    db = get_db()  # this connects to our Firestore database
    logger.debug("NPO Save")    
    # TODO: In this current form, you will overwrite any information that matches the same NPO name

    doc_id = uuid.uuid1().hex

    name = json["name"]
    email = json["email"]
    npoName = json["npoName"]
    slack_channel = json["slack_channel"]
    website = json["website"]
    description = json["description"]
    temp_problem_statements = json["problem_statements"]
    

    # We need to convert this from just an ID to a full object
    # Ref: https://stackoverflow.com/a/59394211
    problem_statements = []
    for ps in temp_problem_statements:
        problem_statements.append(db.collection("problem_statements").document(ps))
     
    collection = db.collection('nonprofits')
    
    insert_res = collection.document(doc_id).set({
        "contact_email": [email], # TODO: Support more than one email
        "contact_people": [name], # TODO: Support more than one name
        "name": npoName,
        "slack_channel" :slack_channel,
        "website": website,
        "description": description,
        "problem_statements": problem_statements
    })

    logger.debug(f"Insert Result: {insert_res}")

    return Message(
        "Saved NPO"
    )

def clear_cache():
    doc_to_json.cache_clear()
    get_single_hackathon_event.cache_clear()
    get_single_hackathon_id.cache_clear()
    

@limits(calls=100, period=ONE_MINUTE)
def remove_npo(json):
    logger.debug("Start NPO Delete")    
    doc_id = json["id"]
    db = get_db()  # this connects to our Firestore database
    doc = db.collection('nonprofits').document(doc_id)
    if doc:
        send_slack_audit(action="remove_npo", message="Removing", payload=doc.get().to_dict())
        doc.delete()

    # TODO: Add a way to track what has been deleted
    # Either by calling Slack or by using another DB/updating the DB with a hidden=True flag, etc.

    logger.debug("End NPO Delete")
    return Message(
        "Delete NPO"
    )


@limits(calls=100, period=ONE_MINUTE)
def link_problem_statements_to_events(json):    
    # JSON format should be in the format of
    # problemStatementId -> [ <eventTitle1>|<eventId1>, <eventTitle2>|<eventId2> ]
    logger.debug(f"Linking payload {json}")
    
    db = get_db()  # this connects to our Firestore database
    data = json["mapping"]
    for problemId, eventList in data.items():
        problem_statement_doc = db.collection(
            'problem_statements').document(problemId)
        
        eventObsList = []
        
        for event in eventList:
            logger.info(f"Checking event: {event}")
            if "|" in event:
                eventId = event.split("|")[1]
            else:
                eventId = event
            event_doc = db.collection('hackathons').document(eventId)
            eventObsList.append(event_doc)

        logger.info(f" Events to add: {eventObsList}")
        problem_result = problem_statement_doc.update({
            "events": eventObsList
        });
        
    clear_cache()

    return Message(
        "Updated Problem Statement to Event Associations"
    )

    

@limits(calls=100, period=ONE_MINUTE)
def update_npo(json):
    db = get_db()  # this connects to our Firestore database

    logger.debug("Clearing cache")    
    clear_cache()

    logger.debug("Done Clearing cache")
    logger.debug("NPO Edit")
    send_slack_audit(action="update_npo", message="Updating", payload=json)
    
    doc_id = json["id"]
    temp_problem_statements = json["problem_statements"]

    doc = db.collection('nonprofits').document(doc_id)

    # We need to convert this from just an ID to a full object
    # Ref: https://stackoverflow.com/a/59394211
    problem_statements = []
    for ps in temp_problem_statements:
        problem_statements.append(db.collection(
            "problem_statements").document(ps))
    

    update_result = doc.update({      
        "problem_statements": problem_statements
    })

    logger.debug(f"Update Result: {update_result}")

    return Message(
        "Updated NPO"
    )


@limits(calls=50, period=ONE_MINUTE)
def save_hackathon(json):
    db = get_db()  # this connects to our Firestore database
    logger.debug("Hackathon Save")
    send_slack_audit(action="save_hackathon", message="Saving", payload=json)
    # TODO: In this current form, you will overwrite any information that matches the same NPO name

    doc_id = uuid.uuid1().hex

    devpost_url = json["devpost_url"]
    location = json["location"]
    
    start_date = json["start_date"]
    end_date = json["end_date"]
    event_type = json["event_type"]
    image_url = json["image_url"]
    
    temp_nonprofits = json["nonprofits"]
    temp_teams = json["teams"]

    # We need to convert this from just an ID to a full object
    # Ref: https://stackoverflow.com/a/59394211
    nonprofits = []
    for ps in temp_nonprofits:
        nonprofits.append(db.collection(
            "nonprofits").document(ps))

    teams = []
    for ps in temp_teams:
        teams.append(db.collection(
            "teams").document(ps))


    collection = db.collection('hackathons')

    insert_res = collection.document(doc_id).set({
        "links":{
            "name":"DevPost",
            "link":"devpost_url"
        },        
        "location": location,
        "start_date": start_date,
        "end_date": end_date,                    
        "type": event_type,
        "image_url": image_url,
        "nonprofits": nonprofits,
        "teams": teams
    })

    logger.debug(f"Insert Result: {insert_res}")

    return Message(
        "Saved Hackathon"
    )


# Ref: https://stackoverflow.com/questions/59138326/how-to-set-google-firebase-credentials-not-with-json-file-but-with-python-dict
# Instead of giving the code a json file, we use environment variables so we don't have to source control a secrets file
cert_env = json.loads(safe_get_env_var("FIREBASE_CERT_CONFIG"))


#We don't want this to be a file, we want to use env variables for security (we would have to check in this file)
#cred = credentials.Certificate("./api/messages/ohack-dev-firebase-adminsdk-hrr2l-933367ee29.json")
cred = credentials.Certificate(cert_env)
# Check if firebase is already initialized
if not firebase_admin._apps:
    firebase_admin.initialize_app(credential=cred)

def save_news(json):
    # Take in Slack message and summarize it using GPT-3.5
    # Make sure these fields exist title, description, links (optional), slack_ts, slack_permalink, slack_channel
    check_fields = ["title", "description", "slack_ts", "slack_permalink", "slack_channel"]
    for field in check_fields:
        if field not in json:
            logger.error(f"Missing field {field} in {json}")
            return Message("Missing field")
        
    cdn_dir = "ohack.dev/news"
    news_image = generate_and_save_image_to_cdn(cdn_dir,json["title"])
    json["image"] = f"{CDN_SERVER}/{cdn_dir}/{news_image}"
    json["last_updated"] = datetime.now().isoformat()
    upsert_news(json)

    logger.info("Updated news successfully")

    get_news.cache_clear()
    logger.info("Cleared cache for get_news")

    return Message("Saved News")

async def save_lead(json):
    token = json["token"]

    # If any field is missing, return False
    if "name" not in json or "email" not in json:
        # Log which fields are missing
        logger.error(f"Missing field name or email {json}")        
        return False
    
    # If name or email length is not long enough, return False
    if len(json["name"]) < 2 or len(json["email"]) < 3:
        # Log
        logger.error(f"Name or email too short name:{json['name']} email:{json['email']}")
        return False
    
    recaptcha_response = requests.post(
        f"https://www.google.com/recaptcha/api/siteverify?secret={google_recaptcha_key}&response={token}")
    recaptcha_response_json = recaptcha_response.json()
    logger.info(f"Recaptcha Response: {recaptcha_response_json}")    

    if recaptcha_response_json["success"] == False:
        return False
    else:
        logger.info("Recaptcha Success, saving...")
        # Save lead to Firestore
        db = get_db()
        collection = db.collection('leads')
        # Remove token from json
        del json["token"]

        # Add timestamp
        json["timestamp"] = datetime.now().isoformat()
        insert_res = collection.add(json) 
        # Log name and email as success
        logger.info(f"Lead saved for {json}")

        # Sent slack message to #ohack-dev-leads
        slack_message = f"New lead! Name:`{json['name']}` Email:`{json['email']}`"
        send_slack(slack_message, "ohack-dev-leads")
        return True

# Create an event loop and run the save_lead function asynchronously
@limits(calls=30, period=ONE_MINUTE)
async def save_lead_async(json):
    await save_lead(json)

@cached(cache=TTLCache(maxsize=100, ttl=32600), key=lambda news_limit, news_id: f"{news_limit}-{news_id}")
def get_news(news_limit=3, news_id=None):
    logger.debug("Get News")
    db = get_db()  # this connects to our Firestore database
    if news_id is not None:
        logger.info(f"Getting single news item for news_id={news_id}")
        collection = db.collection('news')
        doc = collection.document(news_id).get()
        if doc is None:
            return Message({})
        else:
            return Message(doc.to_dict())
    else:
        collection = db.collection('news')
        docs = collection.order_by("slack_ts", direction=firestore.Query.DESCENDING).limit(news_limit).stream()
        results = []
        for doc in docs:
            doc_json = doc.to_dict()
            doc_json["id"] = doc.id
            results.append(doc_json)
        logger.debug(f"Get News Result: {results}")
        return Message(results)