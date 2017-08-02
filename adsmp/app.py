
from __future__ import absolute_import, unicode_literals
from . import exceptions
from .models import Records, ChangeLog, IdentifierMapping
from adsmsg import OrcidClaims, DenormalizedRecord, FulltextUpdate, MetricsRecord, NonBibRecord
from adsmsg.msg import Msg
from adsputils import ADSCelery, create_engine, sessionmaker, scoped_session
from sqlalchemy.orm import load_only as _load_only
from sqlalchemy import Table, bindparam
import adsputils
import json
from adsmp.models import MetricsModel
from adsmp import solr_updater




class ADSMasterPipelineCelery(ADSCelery):

    
    def __init__(self, app_name, *args, **kwargs):
        ADSCelery.__init__(self, app_name, *args, **kwargs)
        
        # this is used for bulk/efficient updates to metrics db
        self._metrics_engine = self._metrics_session = None
        if self._config.get('METRICS_SQLALCHEMY_URL', None):
            self._metrics_engine = create_engine(self._config.get('METRICS_SQLALCHEMY_URL', 'sqlite:///'),
                                   echo=self._config.get('SQLALCHEMY_ECHO', False))
            MetricsModel.metadata.bind = self._metrics_engine
            self._metrics_table = Table('metrics', MetricsModel.metadata)
            self._metrics_conn = self._metrics_engine.conne()
            t = self._metrics_table
            self._metrics_table_update = self._metrics_table.update() \
                .where(t.c.bibcode == bindparam('bibcode')) \
                .values({'refereed': bindparam('refereed'),
                     'rn_citations': bindparam('rn_citations'),
                     'rn_citation_data': bindparam('rn_citation_data'),
                     'rn_citations_hist': bindparam('rn_citations_hist'),
                     'downloads': bindparam('downloads'),
                     'reads': bindparam('reads'),
                     'an_citations': bindparam('an_citations'),
                     'refereed_citation_num': bindparam('refereed_citation_num'),
                     'citation_num': bindparam('citation_num'),
                     'reference_num': bindparam('reference_num'),
                     'citations': bindparam('citations'),
                     'refereed_citations': bindparam('refereed_citations'),
                     'author_num': bindparam('author_num'),
                     'an_refereed_citations': bindparam('an_refereed_citations')})
            self._metrics_table_insert = self._metrics_table.insert() \
                .values({'bibcode': bindparam('bibcode'),
                     'refereed': bindparam('refereed'),
                     'rn_citations': bindparam('rn_citations'),
                     'rn_citation_data': bindparam('rn_citation_data'),
                     'rn_citations_hist': bindparam('rn_citations_hist'),
                     'downloads': bindparam('downloads'),
                     'reads': bindparam('reads'),
                     'an_citations': bindparam('an_citations'),
                     'refereed_citation_num': bindparam('refereed_citation_num'),
                     'citation_num': bindparam('citation_num'),
                     'reference_num': bindparam('reference_num'),
                     'citations': bindparam('citations'),
                     'refereed_citations': bindparam('refereed_citations'),
                     'author_num': bindparam('author_num'),
                     'an_refereed_citations': bindparam('an_refereed_citations')})


    def update_storage(self, bibcode, type, payload):
        if not isinstance(payload, basestring):
            payload = json.dumps(payload)

        with self.session_scope() as session:
            r = session.query(Records).filter_by(bibcode=bibcode).first()
            if r is None:
                r = Records(bibcode=bibcode)
                session.add(r)
            now = adsputils.get_date()
            oldval = None
            if type == 'metadata' or type == 'bib_data':
                oldval = r.bib_data
                r.bib_data = payload
                r.bib_data_updated = now
            elif type == 'nonbib_data':
                oldval = r.nonbib_data
                r.nonbib_data = payload
                r.nonbib_data_updated = now
            elif type == 'orcid_claims':
                oldval = r.orcid_claims
                r.orcid_claims = payload
                r.orcid_claims_updated = now
            elif type == 'fulltext':
                oldval = 'not-stored'
                r.fulltext = payload
                r.fulltext_updated = now
            elif type == 'metrics':
                oldval = 'not-stored'
                r.metrics = payload
                r.metrics_updated = now
            else:
                raise Exception('Unknown type: %s' % type)
            session.add(ChangeLog(key=bibcode, type=type, oldvalue=oldval))
            r.updated = now
            out = r.toJSON()
            session.commit()
            return out


    def delete_by_bibcode(self, bibcode):
        with self.session_scope() as session:
            r = session.query(Records).filter_by(bibcode=bibcode).first()
            if r is not None:
                session.add(ChangeLog(key=bibcode, type='deleted', oldvalue=r.toJSON()))
                session.delete(r)
                session.commit()


    def rename_bibcode(self, old_bibcode, new_bibcode):
        assert old_bibcode and new_bibcode
        assert old_bibcode != new_bibcode

        with self.session_scope() as session:
            r = session.query(Records).filter_by(bibcode=old_bibcode).first()
            if r is not None:

                t = session.query(IdentifierMapping).filter_by(bibcode=old_bibcode).first()
                if t is None:
                    session.add(IdentifierMapping(key=old_bibcode, target=new_bibcode))
                else:
                    while t is not None:
                        target = t.target
                        t.target = new_bibcode
                        t = session.query(IdentifierMapping).filter_by(bibcode=target).first()


                session.add(ChangeLog(key=new_bibcode, type='renamed', oldvalue=r.bibcode, permanent=True))
                r.bibcode = new_bibcode
                session.commit()


    def get_record(self, bibcode, load_only=None):
        if isinstance(bibcode, list):
            out = []
            with self.session_scope() as session:
                q = session.query(Records).filter(Records.bibcode.in_(bibcode))
                if load_only:
                    q = q.options(_load_only(*load_only))
                for r in q.all():
                    out.append(r.toJSON(load_only=load_only))
            return out
        else:
            with self.session_scope() as session:
                q = session.query(Records).filter_by(bibcode=bibcode)
                if load_only:
                    q = q.options(_load_only(*load_only))
                r = q.first()
                if r is None:
                    return None
                return r.toJSON(load_only=load_only)


    def update_processed_timestamp(self, bibcode):
        with self.session_scope() as session:
            r = session.query(Records).filter_by(bibcode=bibcode).first()
            if r is None:
                raise Exception('Cant find bibcode {0} to update timestamp'.format(bibcode))
            r.processed = adsputils.get_date()
            session.commit()


    def get_changelog(self, bibcode):
        out = []
        with self.session_scope() as session:
            r = session.query(Records).filter_by(bibcode=bibcode).first()
            if r is not None:
                out.append(r.toJSON())
            to_collect = [bibcode]
            while len(to_collect):
                for x in session.query(IdentifierMapping).filter_by(key=to_collect.pop()).all():
                    if x.type == 'renamed':
                        to_collect.append(x.oldvalue)
                    out.append(x.toJSON())
        return out

    def get_msg_type(self, msg):
        """Identifies the type of this supplied message.

        :param: Protobuf instance
        :return: str
        """

        if isinstance(msg, OrcidClaims):
            return 'orcid_claims'
        elif isinstance(msg, DenormalizedRecord):
            return 'metadata'
        elif isinstance(msg, FulltextUpdate):
            return 'fulltext'
        elif isinstance(msg, NonBibRecord):
            return 'nonbib_data'
        elif isinstance(msg, MetricsRecord):
            return 'metrics'
        else:
            raise exceptions.IgnorableException('Unkwnown type {0} submitted for update'.format(repr(msg)))


    
    def get_msg_status(self, msg):
        """Identifies the type of this supplied message.

        :param: Protobuf instance
        :return: str
        """

        if isinstance(msg, Msg):
            status = msg.status
            if status == 1:
                return 'deleted'
            else:
                return 'active'
        else:
            return 'unknown'
        
    

    def reindex(self, solr_docs, batch_insert, batch_update, solr_urls):
        """Sends documents to solr and to Metrics DB.
        
        :param: solr_docs - list of json objects (solr documents)
        :param: batch_insert - list of json objects to insert into the metrics
        :param: batch_update - list of json objects to update in metrics db
        :param: solr_urls - list of strings, solr servers.
        """
        
        out = solr_updater.update_solr(solr_docs, solr_urls, ignore_errors=True)
        errs = [x for x in out if x != 200]
        
        if len(errs) == 0:
            self._mark_processed(solr_docs)
            self._insert_into_metrics_db(batch_insert, batch_update)
        else:
            # recover from erros by inserting docs one by one
            for doc in solr_docs:
                try:
                    solr_updater.update_solr([doc], solr_urls, ignore_errors=False)
                    self.update_processed_timestamp(doc['bibcode'])
                except:
                    failed_bibcode = doc['bibcode']
                    self.logger.error('Failed posting data to %s\noffending payload: %s', solr_urls, doc)
                    
                    # when solr_urls > 1, some of the servers may have successfully indexed
                    # but here we are refusing to pass data to metrics db; this seems the 
                    # right choice because there is only one metrics db (but if we had many,
                    # then we could differentiate) 
                    
                    batch_insert = filter(lambda x: x['bibcode'] != failed_bibcode, batch_insert)
                    batch_update = filter(lambda x: x['bibcode'] != failed_bibcode, batch_update)
                    
            self._insert_into_metrics_db(batch_insert, batch_update)

    
    def _mark_processed(self, solr_docs):
        bibcodes = [x['bibcode'] for x in solr_docs]
        now = adsputils.get_date()
        with self.session_scope() as session:
            session.query(Records).filter(Records.bibcode == bibcodes).update({'processed': now})
            session.commit()


    def _insert_into_metrics_db(self, batch_insert, batch_update):
        """Inserts data into the metrics DB."""
        if not self._metrics_table:
            raise Exception('You cant do this! Missing METRICS_SQLALACHEMY_URL?')
        
        # Note: PSQL v9.5 has UPSERT statements, the older versions don't have
        # efficient UPDATE ON DUPLICATE INSERT .... so we do it twice 
        self._metrics_conn.execute(self._metrics_table_insert, batch_insert)
        self._metrics_conn.execute(self._metrics_table_insert, batch_update)            