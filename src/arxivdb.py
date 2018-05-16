#!/usr/bin/env python

'''
arxivdb - Waqas Bhatti (wbhatti@astro.princeton.edu) - Jun 2014

Contains utilities for astroph-coffee server SQLite database manipulation for
inserting/modifying arXiv papers.

'''

try:
    from pysqlite2 import dbapi2 as sqlite3
except:
    print("can't find internal pysqlite2, falling back to Python sqlite3 "
          "full-text search may not work right "
          "if your sqlite3.sqlite3_version is old (< 3.8.6 or so)")
    import sqlite3

import os
import os.path
import ConfigParser
from datetime import datetime, date, timedelta
from pytz import utc
import re

from tornado.escape import squeeze

# for matching local author names
from fuzzywuzzy import process

# to get rid of parens in author names
# these are applied in order

# this should turn:
# "author1 (1, 2, & 3), author1 (1, 2, and 3), ((1) blah1, (2) blah2)"
# into:
# "author1, author2"
affil_regex1 = re.compile(r'\([0-9, &and]+\)')

# this gets rid of patterns like (blah)
affil_regex2 = re.compile(r'\([^)]*\)')


CONF = ConfigParser.ConfigParser()
CONF.read('conf/astroph.conf')

DBPATH = CONF.get('sqlite3','database')

AFFIL_TAGS = CONF.get('localauthors','special_affil_tags')
AFFIL_DEFS = CONF.get('localauthors','special_affil_defs')
AFFIL_DICT = {x.strip():y.strip()
              for x,y in zip(AFFIL_TAGS.split(','), AFFIL_DEFS.split(','))}

RESERVE_INTERVAL_DAYS = int(CONF.get('times','reserve_interval_days'))



def opendb():
    '''
    This just opens a connection to the database and returns it + a cursor.

    '''

    db = sqlite3.connect(
        DBPATH, detect_types=sqlite3.PARSE_DECLTYPES|sqlite3.PARSE_COLNAMES
        )

    cur = db.cursor()

    return db, cur


## LOCAL AUTHORS

def get_local_authors_from_db(database=None):
    '''
    This just pulls out the authors from the local_authors table.

    Normalizes the form so they can be matched against the paper authors.

    '''

    # open the database if needed and get a cursor
    if not database:
        database, cursor = opendb()
        closedb = True
    else:
        cursor = database.cursor()
        closedb = False

    # get all local authors first
    query = 'select author, email from local_authors'
    cursor.execute(query)
    rows = cursor.fetchall()
    if rows and len(rows) > 0:
        local_authors, author_emails = list(zip(*rows))
        local_authors = [x.lower() for x in local_authors]
        local_authors = [x.replace('.',' ') for x in local_authors]
        local_authors = [squeeze(x) for x in local_authors]

        # this contains firstinitial-lastname pairs
        local_author_fnames = [x.split() for x in local_authors]
        local_author_fnames = [''.join([x[0][0],x[-1]])
                               for x in local_author_fnames]
        local_authors = [x.replace(' ','') for x in local_authors]

    else:
        local_authors, local_author_fnames, author_emails = [], [], []

    # at the end, close the cursor and DB connection
    if closedb:
        cursor.close()
        database.close()

    return local_authors, local_author_fnames, author_emails



def force_localauthor_tag(arxivid,
                          local_author_indices,
                          specaffils=None,
                          database=None):
    '''This is a function used to correct the listing if the coffee-server
    misses any local authors the first time it retrieves the day's papers.

    '''

    # open the database if needed and get a cursor
    if not database:
        database, cursor = opendb()
        closedb = True
    else:
        cursor = database.cursor()
        closedb = False

    if specaffils is not None:

        query = ("update arxiv set local_authors = 1, "
                 "local_author_indices = ?, "
                 "local_author_specaffils = ? "
                 "where arxiv_id = ?")
        params = (','.join(['%s' % x for x in local_author_indices]),
                  '/'.join(['%s' % x for x in specaffils]),
                  arxivid)

    else:

        query = ("update arxiv set local_authors = 1, local_author_indices = ? "
                 "where arxiv_id = ?")
        params = (','.join(['%s' % x for x in local_author_indices]), arxivid)

    cursor.execute(query, params)

    database.commit()

    # at the end, close the cursor and DB connection
    if closedb:
        cursor.close()
        database.close()



def force_localauthor_untag(arxivid, database=None):
    '''This is a function used to correct the listing if thee coffee-server
    mistakenly tags a paper as having local authors.

    '''

    # open the database if needed and get a cursor
    if not database:
        database, cursor = opendb()
        closedb = True
    else:
        cursor = database.cursor()
        closedb = False

    query = ("update arxiv set local_authors = 0, "
             "local_author_indices = '', "
             "local_author_specaffils = '' "
             "where arxiv_id = ?")
    params = (arxivid, )
    cursor.execute(query, params)

    database.commit()

    # at the end, close the cursor and DB connection
    if closedb:
        cursor.close()
        database.close()


def strip_affils(authorstr, subchar=','):
    '''
    This tries to strip author affils from authorstr.

    As far as I can test, I think this handles:

    author (affil, ...)
    author (1) ... ((1) affil, ....)
    author (1, 2) ... ((1) affil, ....)
    author (1 & 2) ... ((1) affil, ....)
    author (1 and 2) ... ((1) affil, ....)

    I don't think it can handle:

    author (1) ... (1) affil, ...

    but I haven't seen these in use (yet).

    '''

    initial = authorstr.replace("Authors: ","")
    prelim = affil_regex1.sub(subchar, initial)
    intermed = affil_regex2.sub(subchar, prelim)
    cleaned = intermed.split(',')
    final = [x.strip() for x in cleaned if len(x) > 1]

    return final



def tag_local_authors(arxiv_date,
                      database=None,
                      firstname_match_threshold=99,
                      fullname_match_threshold=99,
                      update_db=False,
                      verbose=False):

    '''
    This finds all local authors for all papers on the date arxiv_date and tags
    the rows for them in the DB.

    '''

    # open the database if needed and get a cursor
    if not database:
        database, cursor = opendb()
        closedb = True
    else:
        cursor = database.cursor()
        closedb = False

    # get all local authors first and normalize their form
    local_authors, local_author_fnames, local_emails = (
        get_local_authors_from_db(database=database)
    )

    if len(local_authors) > 0:

        # get all the authors for this date
        query = 'select arxiv_id, authors from arxiv where utcdate = date(?)'
        query_params = (arxiv_date,)
        cursor.execute(query, query_params)
        rows = cursor.fetchall()

        if rows and len(rows) > 0:

            local_author_articles = []

            for row in rows:

                paper_authors = row[1]

                # get rid of the affiliations for matching to local authors
                paper_authors = strip_affils(paper_authors)

                # we'll save this initial cleaned version back to the database
                # for local matched papers so all the author indices line up
                # correctly
                cleaned_paper_authors = paper_authors[::]

                if verbose:
                    print('%s authors: %s' % (row[0],
                                              repr(cleaned_paper_authors)))

                # normalize these names so we can compare them more robustly to
                # the local authors
                paper_authors = [x.lower().strip() for x in paper_authors]
                paper_authors = [x.strip() for x in paper_authors if len(x) > 1]
                paper_authors = [x.replace('.',' ') for x in paper_authors]
                paper_authors = [squeeze(x) for x in paper_authors]

                paper_author_fnames = [x.split() for x in paper_authors]
                paper_author_fnames = [''.join([x[0][0],x[-1]]) for x
                                      in paper_author_fnames]
                paper_authors = [x.replace(' ','') for x in paper_authors]

                if verbose:
                    print("%s normalized authors: %s" % (row[0],
                                                         repr(paper_authors)))


                local_matched_author_inds = []
                local_matched_author_affils = []

                # match to the flastname first, then if that works, try another
                # match with fullname. if both work, then we accept this as a
                # local author match
                for paper_author, paper_fname, paper_author_ind in zip(
                        paper_authors,
                        paper_author_fnames,
                        range(len(paper_authors))
                ):

                    matched_author_fname = process.extractOne(
                        paper_fname,
                        local_author_fnames,
                        score_cutoff=firstname_match_threshold
                    )

                    matched_author_full = process.extractOne(
                        paper_author,
                        local_authors,
                        score_cutoff=fullname_match_threshold
                    )


                    if matched_author_fname and matched_author_full:

                        print(
                            '%s: %s, matched paper author: %s '
                            'to local author: %s. '
                            'first name score: %s, full name score: %s' % (
                                row[0],
                                paper_authors,
                                paper_author,
                                matched_author_full[0],
                                matched_author_fname[1],
                                matched_author_full[1],
                            )
                        )

                        # update the paper author index column so we can
                        # highlight them in the frontend
                        local_matched_author_inds.append(paper_author_ind)

                        # also update the affilation tag for this author
                        local_authind = local_authors.index(
                            matched_author_full[0]
                        )

                        # get the corresponding email
                        local_matched_email = local_emails[local_authind]

                        # split to get the affil tag
                        local_matched_affil = (
                            local_matched_email.split('@')[-1]
                        )

                        if local_matched_affil in AFFIL_DICT:

                            local_matched_author_affils.append(
                                AFFIL_DICT[local_matched_affil]
                            )

                        # now that we have all the special affils, compress
                        # them into only the unique ones
                        local_matched_author_affils = list(set(
                            local_matched_author_affils
                        ))
                #
                # done with all authors for this paper
                #

                # now update the info for this paper
                if len(local_matched_author_inds) > 0 and update_db:

                    # arxivid of this article that has local authors
                    local_author_articles.append((row[0]))

                    # these encode the positions of the local authors in the
                    # author list
                    local_author_indices = (
                        ','.join(['%s' % x for x in local_matched_author_inds])
                    )
                    local_author_special_affils = ','.join(
                        local_matched_author_affils
                    )

                    cursor.execute(
                        'update arxiv set '
                        'authors = ?, '
                        'local_authors = ?, '
                        'local_author_indices = ?, '
                        'local_author_specaffils = ? '
                        'where '
                        'arxiv_id = ?',
                        (','.join(cleaned_paper_authors),
                         True,
                         local_author_indices,
                         local_author_special_affils,
                         row[0])
                    )


            #
            # done with all papers for the day
            #

            # commit the transaction at the end
            if update_db:
                database.commit()

            return local_author_articles

        else:

            print('no articles for this date')
            return False

    else:

        print('no local authors defined')
        return False

    # at the end, close the cursor and DB connection
    if closedb:
        cursor.close()
        database.close()



## INSERTING ARTICLES

def insert_articles(arxiv,
                    database=None,
                    tag_locals=True,
                    fullname_match_threshold=99,
                    firstname_match_threshold=99,
                    verbose=False):
    '''
    This inserts all articles in an arxivdict created by
    arxivutils.grab_arxiv_update into the astroph-coffee server database.

    '''

    # open the database if needed and get a cursor
    if not database:
        database, cursor = opendb()
        closedb = True
    else:
        cursor = database.cursor()
        closedb = False

    # make sure we know that the dt is in UTC
    arxiv_dt = arxiv['utc']
    if not arxiv_dt.tzinfo:
        arxiv_dt = arxiv_dt.replace(tzinfo=utc)


    papers = arxiv['papers']
    crosslists = arxiv['crosslists']

    query = ("insert or replace into arxiv (utctime, utcdate, "
             "day_serial, title, article_type,"
             "arxiv_id, authors, comments, abstract, link, pdf, "
             "nvotes, voters, presenters, local_authors, reserved) values "
             "(?,?, ?,?,?, ?,?,?,?,?,?, ?,?,?,?, 0)")

    try:

        for key in papers:

            if verbose:
                print('inserting astronomy article %s: %s' %
                      (key, papers[key]['title']))

            u_title = unicode(papers[key]['title'])
            u_authors = unicode(','.join(papers[key]['authors']))
            u_comments = unicode(papers[key]['comments'])
            u_abstract = unicode(papers[key]['abstract'])

            # get rid of the initial 'Authors: ' bit
            u_authors = u_authors.replace('Authors:','',1)
            u_authors = u_authors.strip()

            params = (arxiv_dt,
                      arxiv_dt.date(),
                      key,
                      u_title,
                      'astronomy',
                      papers[key]['arxiv'],
                      u_authors,
                      u_comments,
                      u_abstract,
                      'http://arxiv.org%s' % papers[key]['link'],
                      'http://arxiv.org%s' % papers[key]['pdf'],
                      0,
                      '',
                      '',
                      False)
            cursor.execute(query, params)

        for key in crosslists:

            if verbose:
                print('inserting cross-list article %s: %s' %
                      (key, crosslists[key]['title']))

            params = (arxiv_dt,
                      arxiv_dt.date(),
                      key,
                      unicode(crosslists[key]['title']),
                      'crosslists',
                      crosslists[key]['arxiv'],
                      unicode(','.join(crosslists[key]['authors'])),
                      unicode(crosslists[key]['comments']),
                      unicode(crosslists[key]['abstract']),
                      'http://arxiv.org%s' % crosslists[key]['link'],
                      'http://arxiv.org%s' % crosslists[key]['pdf'],
                      0,
                      '',
                      '',
                      False)
            cursor.execute(query, params)

        database.commit()

    except Exception as e:

        print('could not insert articles into the DB, error was %s' % e)
        database.rollback()

    # once we're done with the inserting articles bit, tag all local authors if
    # directed to do so
    if tag_locals:
        tag_local_authors(arxiv_dt.date(),
                          database=database,
                          firstname_match_threshold=firstname_match_threshold,
                          fullname_match_threshold=fullname_match_threshold,
                          update_db=True,
                          verbose=verbose)


    # at the end, close the cursor and DB connection
    if closedb:
        cursor.close()
        database.close()


## RETRIEVING ARTICLES

def get_articles_for_listing(utcdate=None,
                             database=None,
                             astronomyonly=False):
    '''

    This grabs all articles from the database for the given date for listing at
    /astroph-coffee/papers. Cross-lists are included in other_articles.

    Three lists are returned:

    (local_articles,
     voted_articles,
     other_articles)

    '''

    # open the database if needed and get a cursor
    if not database:
        database, cursor = opendb()
        closedb = True
    else:
        cursor = database.cursor()
        closedb = False

    # if no utcdate is provided, find the latest utcdate and use that
    if not utcdate:

        query = 'select utcdate from arxiv order by utcdate desc limit 1'
        cursor.execute(query)
        row = cursor.fetchone()
        utcdate = row[0].strftime('%Y-%m-%d')


    local_articles, voted_articles, other_articles = [], [], []
    reserved_articles = []

    articles_excluded_from_voted = []
    articles_excluded_from_other = []

    # deal with the local articles first
    if astronomyonly:
        query = ("select arxiv_id, day_serial, title, article_type, "
                 "authors, comments, abstract, link, pdf, nvotes, voters, "
                 "presenters, local_authors, reserved, reservers, "
                 "local_author_indices, local_author_specaffils from arxiv "
                 "where "
                 "utcdate = date(?) and local_authors = 1 "
                 "and article_type = 'astronomy' "
                 "order by nvotes desc")
    else:
        query = ("select arxiv_id, day_serial, title, article_type, "
                 "authors, comments, abstract, link, pdf, nvotes, voters, "
                 "presenters, local_authors, reserved, reservers, "
                 "local_author_indices, local_author_specaffils from arxiv "
                 "where "
                 "utcdate = date(?) and local_authors = 1 "
                 "order by nvotes desc")

    query_params = (utcdate,)
    cursor.execute(query, query_params)
    rows = cursor.fetchall()

    if len(rows) > 0:
        for row in rows:
            local_articles.append(list(row))
            articles_excluded_from_other.append(row[0])
            articles_excluded_from_voted.append(row[0])

    # deal with articles that have votes next
    # finally deal with the other articles
    if len(articles_excluded_from_voted) > 0:

        if astronomyonly:
            query = ("select arxiv_id, day_serial, title, article_type, "
                     "authors, comments, abstract, link, pdf, nvotes, voters, "
                     "presenters, local_authors, reserved, reservers, "
                     "local_author_indices, local_author_specaffils from arxiv "
                     "where "
                     "utcdate = date(?) and "
                     "arxiv_id not in ({exclude_list}) "
                     "and nvotes > 0 "
                     "and article_type = 'astronomy' "
                     "order by nvotes desc")
        else:
            query = ("select arxiv_id, day_serial, title, article_type, "
                     "authors, comments, abstract, link, pdf, nvotes, voters, "
                     "presenters, local_authors, reserved, reservers, "
                     "local_author_indices, local_author_specaffils from arxiv "
                     "where "
                     "utcdate = date(?) and "
                     "arxiv_id not in ({exclude_list}) "
                     "and nvotes > 0 "
                     "order by nvotes desc")

        placeholders = ', '.join('?' for x in articles_excluded_from_voted)
        query = query.format(exclude_list=placeholders)
        query_params = tuple([utcdate] + articles_excluded_from_voted)

    else:

        if astronomyonly:
            query = ("select arxiv_id, day_serial, title, article_type, "
                     "authors, comments, abstract, link, pdf, nvotes, voters, "
                     "presenters, local_authors, reserved, reservers, "
                     "local_author_indices, local_author_specaffils from arxiv "
                     "where "
                     "utcdate = date(?) "
                     "and nvotes > 0 "
                     "and article_type = 'astronomy' "
                     "order by nvotes desc")
        else:
            query = ("select arxiv_id, day_serial, title, article_type, "
                     "authors, comments, abstract, link, pdf, nvotes, voters, "
                     "presenters, local_authors, reserved, reservers, "
                     "local_author_indices, local_author_specaffils from arxiv "
                     "where "
                     "utcdate = date(?) "
                     "and nvotes > 0 "
                     "order by nvotes desc")

        query_params = (utcdate,)

    cursor.execute(query, query_params)
    rows = cursor.fetchall()

    if len(rows) > 0:
        for row in rows:
            voted_articles.append(row)
            articles_excluded_from_other.append(row[0])

    # get the historical reserved articles list
    if astronomyonly:
        query = ("select arxiv_id, day_serial, title, article_type, "
                 "authors, comments, abstract, link, pdf, nvotes, voters, "
                 "presenters, local_authors, reserved, reservers, utcdate, "
                 "local_author_indices, local_author_specaffils "
                 "from arxiv where "
                 "(utcdate between date(?) and date(?)) and reserved = 1 "
                 "and article_type = 'astronomy' "
                 "order by arxiv_id desc")
    else:
        query = ("select arxiv_id, day_serial, title, article_type, "
                 "authors, comments, abstract, link, pdf, nvotes, voters, "
                 "presenters, local_authors, reserved, reservers, utcdate, "
                 "local_author_indices, local_author_specaffils "
                 "from arxiv where "
                 "(utcdate between date(?) and date(?)) and reserved = 1 "
                 "order by arxiv_id desc")

    # fetch all reserved articles up to RESERVE_INTERVAL_DAYS older than the
    # given utcdate
    given_dt = datetime.strptime(utcdate,'%Y-%m-%d')
    earliest_dt = given_dt - timedelta(days=RESERVE_INTERVAL_DAYS)
    earliest_utcdate = earliest_dt.strftime('%Y-%m-%d')

    query_params = (earliest_utcdate, utcdate)
    cursor.execute(query, query_params)
    rows = cursor.fetchall()

    if len(rows) > 0:
        for row in rows:
            reserved_articles.append(row)
            articles_excluded_from_other.append(row[0])


    # finally deal with the other articles
    if len(articles_excluded_from_other) > 0:

        if astronomyonly:
            query = ("select arxiv_id, day_serial, title, article_type, "
                     "authors, comments, abstract, link, pdf, nvotes, voters, "
                     "presenters, local_authors, reserved, reservers, "
                     "local_author_indices, local_author_specaffils from arxiv "
                     "where "
                     "utcdate = date(?) and "
                     "arxiv_id not in ({exclude_list}) "
                     "and article_type = 'astronomy' "
                     "order by day_serial asc")
        else:
            query = ("select arxiv_id, day_serial, title, article_type, "
                     "authors, comments, abstract, link, pdf, nvotes, voters, "
                     "presenters, local_authors, reserved, reservers, "
                     "local_author_indices, local_author_specaffils from arxiv "
                     "where "
                     "utcdate = date(?) and "
                     "arxiv_id not in ({exclude_list}) "
                     "order by article_type asc, day_serial asc")

        placeholders = ', '.join('?' for x in articles_excluded_from_other)
        query = query.format(exclude_list=placeholders)
        query_params = tuple([utcdate] + articles_excluded_from_other)

    else:

        if astronomyonly:
            query = ("select arxiv_id, day_serial, title, article_type, "
                     "authors, comments, abstract, link, pdf, nvotes, voters, "
                     "presenters, local_authors, reserved, reservers, "
                     "local_author_indices, local_author_specaffils from arxiv "
                     "where "
                     "utcdate = date(?) "
                     "and article_type = 'astronomy' "
                     "order by day_serial asc")
        else:
            query = ("select arxiv_id, day_serial, title, article_type, "
                     "authors, comments, abstract, link, pdf, nvotes, voters, "
                     "presenters, local_authors, reserved, reservers, "
                     "local_author_indices, local_author_specaffils from arxiv "
                     "where "
                     "utcdate = date(?) "
                     "order by article_type asc, day_serial asc")

        query_params = (utcdate,)

    cursor.execute(query, query_params)
    rows = cursor.fetchall()

    if len(rows) > 0:
        for row in rows:
            other_articles.append(row)


    # at the end, close the cursor and DB connection
    if closedb:
        cursor.close()
        database.close()

    return [utcdate,
            list(local_articles),
            voted_articles,
            other_articles,
            reserved_articles]



def get_articles_for_voting(database=None,
                            astronomyonly=False):
    '''
    This grabs all articles from the database for today's date to show for
    voting. The articles are sorted in arxiv_id order with papers and
    cross_lists returned separately.

    '''

    # open the database if needed and get a cursor
    if not database:
        database, cursor = opendb()
        closedb = True
    else:
        cursor = database.cursor()
        closedb = False

    # this is today's date
    utcdate = datetime.now(tz=utc).strftime('%Y-%m-%d')


    local_articles, voted_articles, other_articles = [], [], []
    reserved_articles = []

    articles_excluded_from_voted = []
    articles_excluded_from_other = []

    # deal with the local articles first
    if astronomyonly:
        query = ("select arxiv_id, day_serial, title, article_type, "
                 "authors, comments, abstract, link, pdf, nvotes, voters, "
                 "presenters, local_authors, reserved, reservers, "
                 "local_author_indices, local_author_specaffils from arxiv "
                 "where "
                 "utcdate = date(?) and local_authors = 1 "
                 "and article_type = 'astronomy' "
                 "order by nvotes desc")
    else:
        query = ("select arxiv_id, day_serial, title, article_type, "
                 "authors, comments, abstract, link, pdf, nvotes, voters, "
                 "presenters, local_authors, reserved, reservers, "
                 "local_author_indices, local_author_specaffils from arxiv "
                 "where "
                 "utcdate = date(?) and local_authors = 1 "
                 "order by nvotes desc")

    query_params = (utcdate,)
    cursor.execute(query, query_params)
    rows = cursor.fetchall()

    if len(rows) > 0:
        for row in rows:
            local_articles.append(list(row))
            articles_excluded_from_other.append(row[0])
            articles_excluded_from_voted.append(row[0])

    # deal with articles that have votes next
    # finally deal with the other articles
    if len(articles_excluded_from_voted) > 0:

        if astronomyonly:
            query = ("select arxiv_id, day_serial, title, article_type, "
                     "authors, comments, abstract, link, pdf, nvotes, voters, "
                     "presenters, local_authors, reserved, reservers, "
                     "local_author_indices, local_author_specaffils from arxiv "
                     "where "
                     "utcdate = date(?) and "
                     "arxiv_id not in ({exclude_list}) "
                     "and nvotes > 0 "
                     "and article_type = 'astronomy' "
                     "order by nvotes desc")
        else:
            query = ("select arxiv_id, day_serial, title, article_type, "
                     "authors, comments, abstract, link, pdf, nvotes, voters, "
                     "presenters, local_authors, reserved, reservers, "
                     "local_author_indices, local_author_specaffils from arxiv "
                     "where "
                     "utcdate = date(?) and "
                     "arxiv_id not in ({exclude_list}) "
                     "and nvotes > 0 "
                     "order by nvotes desc")

        placeholders = ', '.join('?' for x in articles_excluded_from_voted)
        query = query.format(exclude_list=placeholders)
        query_params = tuple([utcdate] + articles_excluded_from_voted)

    else:

        if astronomyonly:
            query = ("select arxiv_id, day_serial, title, article_type, "
                     "authors, comments, abstract, link, pdf, nvotes, voters, "
                     "presenters, local_authors, reserved, reservers, "
                     "local_author_indices, local_author_specaffils from arxiv "
                     "where "
                     "utcdate = date(?) "
                     "and nvotes > 0 "
                     "and article_type = 'astronomy' "
                     "order by nvotes desc")
        else:
            query = ("select arxiv_id, day_serial, title, article_type, "
                     "authors, comments, abstract, link, pdf, nvotes, voters, "
                     "presenters, local_authors, reserved, reservers, "
                     "local_author_indices, local_author_specaffils from arxiv "
                     "where "
                     "utcdate = date(?) "
                     "and nvotes > 0 "
                     "order by nvotes desc")

        query_params = (utcdate,)

    cursor.execute(query, query_params)
    rows = cursor.fetchall()

    if len(rows) > 0:
        for row in rows:
            voted_articles.append(row)
            articles_excluded_from_other.append(row[0])

    # get the historical reserved articles list
    if astronomyonly:
        query = ("select arxiv_id, day_serial, title, article_type, "
                 "authors, comments, abstract, link, pdf, nvotes, voters, "
                 "presenters, local_authors, reserved, reservers, utcdate, "
                 "local_author_indices, local_author_specaffils "
                 "from arxiv where "
                 "(utcdate between date(?) and date(?)) and reserved = 1 "
                 "and article_type = 'astronomy' "
                 "order by arxiv_id desc")
    else:
        query = ("select arxiv_id, day_serial, title, article_type, "
                 "authors, comments, abstract, link, pdf, nvotes, voters, "
                 "presenters, local_authors, reserved, reservers, utcdate, "
                 "local_author_indices, local_author_specaffils "
                 "from arxiv where "
                 "(utcdate between date(?) and date(?)) and reserved = 1 "
                 "order by arxiv_id desc")

    # fetch all reserved articles up to 3 days older than the given utcdate
    given_dt = datetime.strptime(utcdate,'%Y-%m-%d')
    earliest_dt = given_dt - timedelta(days=RESERVE_INTERVAL_DAYS)
    earliest_utcdate = earliest_dt.strftime('%Y-%m-%d')

    query_params = (earliest_utcdate, utcdate)
    cursor.execute(query, query_params)
    rows = cursor.fetchall()

    if len(rows) > 0:
        for row in rows:
            reserved_articles.append(row)
            articles_excluded_from_other.append(row[0])


    # finally deal with the other articles
    if len(articles_excluded_from_other) > 0:

        if astronomyonly:
            query = ("select arxiv_id, day_serial, title, article_type, "
                     "authors, comments, abstract, link, pdf, nvotes, voters, "
                     "presenters, local_authors, reserved, reservers, "
                     "local_author_indices, local_author_specaffils from arxiv "
                     "where "
                     "utcdate = date(?) and "
                     "arxiv_id not in ({exclude_list}) "
                     "and article_type = 'astronomy' "
                     "order by day_serial asc")
        else:
            query = ("select arxiv_id, day_serial, title, article_type, "
                     "authors, comments, abstract, link, pdf, nvotes, voters, "
                     "presenters, local_authors, reserved, reservers, "
                     "local_author_indices, local_author_specaffils from arxiv "
                     "where "
                     "utcdate = date(?) and "
                     "arxiv_id not in ({exclude_list}) "
                     "order by article_type asc, day_serial asc")

        placeholders = ', '.join('?' for x in articles_excluded_from_other)
        query = query.format(exclude_list=placeholders)
        query_params = tuple([utcdate] + articles_excluded_from_other)

    else:

        if astronomyonly:
            query = ("select arxiv_id, day_serial, title, article_type, "
                     "authors, comments, abstract, link, pdf, nvotes, voters, "
                     "presenters, local_authors, reserved, reservers, "
                     "local_author_indices, local_author_specaffils from arxiv "
                     "where "
                     "utcdate = date(?) "
                     "and article_type = 'astronomy' "
                     "order by day_serial asc")
        else:
            query = ("select arxiv_id, day_serial, title, article_type, "
                     "authors, comments, abstract, link, pdf, nvotes, voters, "
                     "presenters, local_authors, reserved, reservers, "
                     "local_author_indices, local_author_specaffils from arxiv "
                     "where "
                     "utcdate = date(?) "
                     "order by article_type asc, day_serial asc")

        query_params = (utcdate,)

    cursor.execute(query, query_params)
    rows = cursor.fetchall()

    if len(rows) > 0:
        for row in rows:
            other_articles.append(row)

    # at the end, close the cursor and DB connection
    if closedb:
        cursor.close()
        database.close()

    return [list(local_articles),
            voted_articles,
            other_articles,
            reserved_articles]


## ARTICLE ARCHIVES
def get_archive_index(start_date=None,
                      end_date=None,
                      database=None):
    '''
    This returns all article archives in reverse date order.

    '''
    # open the database if needed and get a cursor
    if not database:
        database, cursor = opendb()
        closedb = True
    else:
        cursor = database.cursor()
        closedb = False

    query = ("select utcdate, count(*), sum(local_authors), "
             "sum(case when nvotes > 0 then 1 else 0 end) from arxiv "
             "group by utcdate order by utcdate desc")
    cursor.execute(query)
    rows = cursor.fetchall()

    if rows and len(rows) > 0:
        arxivdates, arxivpapers, arxivlocals, arxivvoted = zip(*rows)
    else:
        arxivdates, arxivpapers, arxivlocals, arxivvoted = [], [], [], []

    # at the end, close the cursor and DB connection
    if closedb:
        cursor.close()
        database.close()

    return (arxivdates, arxivpapers, arxivlocals, arxivvoted)


## VOTERS AND PRESENTERS

def record_vote(arxivid, username, vote, database=None):
    '''This records votes for a paper in the DB. vote is 'up' or 'down'. If the
    arxivid doesn't exist, then returns False. If the vote is successfully
    processed, returns the nvotes for the arxivid.

    '''

    # open the database if needed and get a cursor
    if not database:
        database, cursor = opendb()
        closedb = True
    else:
        cursor = database.cursor()
        closedb = False

    returnval = False

    if vote == 'up':
        voteval = 1
    elif vote =='down':
        voteval = -1
    else:
        voteval = 0

    if voteval > 0:
        # votes only count ONCE per article
        query = ("update arxiv set nvotes = (nvotes + ?), "
                 "voters = (voters || ? || ',') "
                 "where arxiv_id = ? and "
                 "voters not like ?")
        query_params = (voteval,
                        username,
                        arxivid,
                        '%{0}%'.format(username))

    elif voteval < 0:
        # votes only count ONCE per article
        query = ("update arxiv set nvotes = (nvotes + ?), "
                 "voters = replace(voters, (? || ','), '') "
                 "where arxiv_id = ? and "
                 "voters like ?")
        query_params = (voteval,
                        username,
                        arxivid,
                        '%{0}%'.format(username))

    else:
        return False


    try:

        cursor.execute(query, query_params)
        database.commit()

        cursor.execute("select nvotes from arxiv where arxiv_id = ?",
                       (arxivid,))
        rows = cursor.fetchone()

        if rows and len(rows) > 0:
            returnval = rows[0]

    except Exception as e:
        raise
        returnval = False

    # at the end, close the cursor and DB connection
    if closedb:
        cursor.close()
        database.close()

    return returnval


def record_reservation(arxivid, username, reservation, database=None):
    '''This records votes for a paper in the DB. reservation is 'reserve' or
    'release'. If the arxivid doesn't exist, then returns False. If the
    reservation is successfully processed, returns the reserved flag for the
    arxivid.

    '''

    # open the database if needed and get a cursor
    if not database:
        database, cursor = opendb()
        closedb = True
    else:
        cursor = database.cursor()
        closedb = False

    returnval = False

    if reservation == 'reserve':

        # reservations only count ONCE per article
        query = ("update arxiv set reserved = 1, "
                 "reservers = ? "
                 "where arxiv_id = ? and "
                 "reservers is null")
        query_params = (username,
                        arxivid)

    elif reservation == 'release':
        # reservations only count ONCE per article
        query = ("update arxiv set reserved = 0, "
                 "reservers = null "
                 "where arxiv_id = ? and "
                 "reservers like ?")
        query_params = (arxivid, '%{0}%'.format(username))

    else:
        return False


    try:

        cursor.execute(query, query_params)
        database.commit()

        cursor.execute("select reserved, reservers from arxiv "
                       "where arxiv_id = ?",
                       (arxivid,))
        rows = cursor.fetchone()

        if rows and len(rows) > 0:
            returnval = rows

    except Exception as e:
        database.rollback()
        raise
        returnval = False

    # at the end, close the cursor and DB connection
    if closedb:
        cursor.close()
        database.close()

    return returnval



def record_edit(arxivid, username, edittype, database=None):
    '''This records edits for a paper in the DB. The edittype is 'islocal' or
    'isnotlocal' for now. If the arxivid doesn't exist, then returns False.

    '''

    # open the database if needed and get a cursor
    if not database:
        database, cursor = opendb()
        closedb = True
    else:
        cursor = database.cursor()
        closedb = False

    returnval = False

    # FIXME: finish this local/nonlocal query
    if edittype == 'islocal':

        # edittypes only count ONCE per article
        query = ("update arxiv set local_authors = 1 "
                 "where arxiv_id = ? ")
        query_params = (arxivid, )

    elif edittype == 'isnotlocal':
        # edittypes only count ONCE per article
        query = ("update arxiv set local_authors = 0 "
                 "where arxiv_id = ? ")
        query_params = (arxivid, )

    else:
        return False


    try:

        cursor.execute(query, query_params)
        database.commit()

        cursor.execute("select arxiv_id, local_authors from arxiv "
                       "where arxiv_id = ? "
                       "",
                       (arxivid,))
        rows = cursor.fetchone()

        if rows and len(rows) > 0:
            returnval = rows

    except Exception as e:
        database.rollback()
        raise
        returnval = False

    # at the end, close the cursor and DB connection
    if closedb:
        cursor.close()
        database.close()

    return returnval



def get_user_reservations(utcdate, username,
                          database=None):
    '''This gets a user's reserved papers.

    Papers are reserved for up to RESERVE_INTERVAL_DAYS, so we check for utcdate
    - RESERVE_INTERVAL_DAYS days here. The frontend uses this function to set
    the current reservation state for the articles on the voting and listing
    pages.

    The voting page shows all reserved papers from everyone up to today's date
    and allows unreserving papers that this user reserved.

    The listing page shows all reserved papers as well, but does not allow
    dereserving (i.e. dereserving only happens during voting periods).

    '''

    # open the database if needed and get a cursor
    if not database:
        database, cursor = opendb()
        closedb = True
    else:
        cursor = database.cursor()
        closedb = False

    # get all the reserved papers by this user for this utcdate -
    # RESERVE_INTERVAL_DAYS
    query = ("select arxiv_id, reservers from arxiv where "
             "(utcdate between ? and ?) and "
             "(reserved = 1)")

    # figure out the oldest date
    given_dt = datetime.strptime(utcdate,'%Y-%m-%d')
    earliest_dt = given_dt - timedelta(days=RESERVE_INTERVAL_DAYS)
    earliest_utcdate = earliest_dt.strftime('%Y-%m-%d')

    params = (earliest_utcdate, utcdate)
    cursor.execute(query, params)
    rows = cursor.fetchall()

    if rows and len(rows) > 0:

        reserved_arxivids = []

        for row in rows:
            paper, reservers = row
            reservers = reservers.split(',')
            if username in reservers:
                reserved_arxivids.append(paper)

    else:

        reserved_arxivids = []


    # at the end, close the cursor and DB connection
    if closedb:
        cursor.close()
        database.close()

    return reserved_arxivids



def get_user_votes(utcdate, username, database=None):

    '''
    This gets a user's votes for all arxivids on the current utcdate. The
    frontend uses this to set the current voting state for the articles on the
    voting page.

    '''

    # open the database if needed and get a cursor
    if not database:
        database, cursor = opendb()
        closedb = True
    else:
        cursor = database.cursor()
        closedb = False


    # get all the voters for this date
    # FIXME: this would be WAY better handled using FTS
    query = ("select arxiv_id, voters from arxiv "
             "where utcdate = ? and nvotes > 0 ")
    query_params = (utcdate,)

    cursor.execute(query, query_params)
    rows = cursor.fetchall()

    if rows and len(rows) > 0:

        voted_arxivids = []

        for row in rows:
            paper, voters = row
            voters = voters.split(',')
            if username in voters:
                voted_arxivids.append(paper)

    else:

        voted_arxivids = []


    # at the end, close the cursor and DB connection
    if closedb:
        cursor.close()
        database.close()

    return voted_arxivids



def modify_presenters(arxivid, presenter, action, database=None):
    '''
    This adds/removes presenters for a paper.

    action = 'add' -> adds person named presenter
    action = 'remove' -> removes person named presenter

    '''

    # open the database if needed and get a cursor
    if not database:
        database, cursor = opendb()
        closedb = True
    else:
        cursor = database.cursor()
        closedb = False





    # at the end, close the cursor and DB connection
    if closedb:
        cursor.close()
        database.close()
