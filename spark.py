from datetime import datetime,time,date,timedelta

import numpy as np

import tornado.ioloop
import tornado.web
import tornado.httpserver
from tornado.concurrent import Future
from tornado import gen
from tornado.ioloop import IOLoop
import tornpsql

from pyspark import SparkContext, SparkConf

from computing import *
from util import *
import blockentropy

fulldataset_chunk_size = 1000

def run_extractmzs(sc, fname, data, nrows, ncols):
	ff = sc.textFile(fname)
	spectra = ff.map(txt_to_spectrum)
	# qres = spectra.map(lambda sp : get_many_groups_total_dict(data, sp)).reduce(join_dicts)
	qres = spectra.map(lambda sp : get_many_groups_total_dict_individual(data, sp)).reduce(reduce_manygroups_dict)
	entropies = [ blockentropy.get_block_entropy_dict(x, nrows, ncols) for x in qres ]
	return (qres, entropies)

def dicts_to_dict(dictresults):
	res_dict = dictresults[0]
	for res in dictresults[1:]:
		res_dict.update({ k : v + res_dict.get(k, 0.0) for k,v in res.iteritems() })
	return res_dict

def run_fulldataset(sc, fname, data, nrows, ncols):
	ff = sc.textFile(fname)
	spectra = ff.map(txt_to_spectrum)
	qres = spectra.map(lambda sp : get_many_groups2d_total_dict_individual(data, sp)).reduce(reduce_manygroups2d_dict_individual)
	entropies = [ [ blockentropy.get_block_entropy_dict(x, nrows, ncols) for x in res ] for res in qres ]
	return (qres, entropies)


class RunSparkHandler(tornado.web.RequestHandler):
	@property
	def db(self):
		return self.application.db

	def result_callback(response):
		my_print("Got response! %s" % response)

	def strings_to_dict(self, stringresults):
		res_dict = { int(x.split(':')[0]) : float(x.split(':')[1]) for x in stringresults[0].split(' ') }
		for res_string in stringresults[1:]:
			res_dict.update({ int(x.split(':')[0]) : float(x.split(':')[1]) + res_dict.get(int(x.split(':')[0]), 0.0) for x in res_string.split(' ') })
		return res_dict

	def insert_job_result_stats(self, formula_ids, num_peaks, stats):
		if len(formula_ids) > 0:
			for stdict in stats:
				if "entropies" in stdict:
					stdict.update({ 'mean_ent' : np.mean(stdict["entropies"]) })
			self.db.query('INSERT INTO job_result_stats VALUES %s' % (
				",".join([ '(%d, %s, %d, \'%s\')' % (self.job_id, formula_ids[i], num_peaks[i], json.dumps(
					stats[i]
				)) for i in xrange(len(formula_ids)) ])
			) )

	def process_res_extractmzs(self, result):
		res_array, entropies = result.get()
		my_print("Got result of job %d with %d peaks" % (self.job_id, len(res_array)))
		if (sum([len(x) for x in res_array]) > 0):
			self.db.query("INSERT INTO job_result_data VALUES %s" %
				",".join(['(%d, %d, %d, %d, %.6f)' % (self.job_id, -1, i, k, v) for i in xrange(len(res_array)) for k,v in res_array[i].iteritems()])
			)
		self.insert_job_result_stats( [ self.formula_id ], [ len(res_array) ], [ {
			"entropies" : entropies,
			"corr_images" : avg_dict_correlation(res_array)
		} ] )

	def process_res_fulldataset(self, result, offset=0):
		res_dicts, entropies = result.get()
		total_nonzero = sum([len(x) for x in res_dicts])
		my_print("Got result of full dataset job %d with %d nonzero spectra" % (self.job_id, total_nonzero))
		if (total_nonzero > 0):
			self.db.query("INSERT INTO job_result_data VALUES %s" %
				",".join(['(%d, %d, %d, %d, %.6f)' % (self.job_id,
					int(self.formulas[i+offset]["id"]), j, k, v)
					for i in xrange(len(res_dicts)) for j in xrange(len(res_dicts[i])) for k,v in res_dicts[i][j].iteritems()])
			)
		self.insert_job_result_stats(
			[ self.formulas[i+offset]["id"] for i in xrange(len(res_dicts)) ],
			[ len(res_dicts[i]) for i in xrange(len(res_dicts)) ],
			[ { "entropies" : entropies[i], "corr_images" : avg_dict_correlation(res_dicts[i]) } for i in xrange(len(res_dicts)) ]
		)

	@gen.coroutine
	def post(self, query_id):
		my_print("called /run/" + query_id)

		self.dataset_id = int(self.get_argument("dataset_id"))
		dataset_params = self.db.query("SELECT filename,nrows,ncols FROM datasets WHERE dataset_id=%d" % self.dataset_id)[0]
		self.nrows = dataset_params["nrows"]
		self.ncols = dataset_params["ncols"]
		self.fname = dataset_params["filename"]
		self.job_id = -1
		## we want to extract m/z values
		if query_id == "extractmzs":
			self.formula_id = self.get_argument("formula_id")
			self.job_type = 0
			tol = 0.01
			peaks = self.db.query("SELECT peaks FROM mz_peaks WHERE formula_id='%s'" % self.formula_id)[0]["peaks"]
			# data = [ [float(x)-tol, float(x)+tol] for x in self.get_argument("data").strip().split(',')]
			data = [ [float(x)-tol, float(x)+tol] for x in peaks]
			my_print("Running m/z extraction for formula id %s" % self.formula_id)
			my_print("Input data: %s" % " ".join([ "[%.3f, %.3f]" % (x[0], x[1]) for x in data]))

			cur_jobs = set(self.application.status.getActiveJobsIds())
			my_print("Current jobs: %s" % cur_jobs)
			result = call_in_background(run_extractmzs, *(self.application.sc, self.fname, data, self.nrows, self.ncols))
			self.spark_job_id = -1
			while self.spark_job_id == -1:
				yield async_sleep(1)
				my_print("Current jobs: %s" % set(self.application.status.getActiveJobsIds()))
				if len(set(self.application.status.getActiveJobsIds()) - cur_jobs) > 0:
					self.spark_job_id = list(set(self.application.status.getActiveJobsIds()) - cur_jobs)[0]
			## if this job hasn't started yet, add it
			if self.job_id == -1:
				self.job_id = self.application.add_job(self.spark_job_id, self.formula_id, self.dataset_id, self.job_type, datetime.now())
			else:
				self.application.jobs[self.job_id]["spark_id"] = self.spark_job_id
			while result.empty():
				yield async_sleep(1)
			self.process_res_extractmzs(result)

		elif query_id == "fulldataset":
			my_print("Running dataset-wise m/z image extraction for dataset id %s" % self.dataset_id)
			self.formula_id = -1
			self.job_type = 1
			prefix = "\t[fullrun %s] " % self.dataset_id
			my_print(prefix + "collecting m/z queries for the run")
			tol = 0.01
			self.formulas = self.db.query("SELECT formula_id as id,peaks FROM mz_peaks")
			mzpeaks = [ x["peaks"] for x in self.formulas]
			data = [ [ [float(x)-tol, float(x)+tol] for x in peaks ] for peaks in mzpeaks ]
			my_print(prefix + "looking for %d peaks" % sum([len(x) for x in data]))
			self.num_chunks = 1 + len(data) / fulldataset_chunk_size
			self.job_id = self.application.add_job(-1, self.formula_id, self.dataset_id, self.job_type, datetime.now(), chunks=self.num_chunks)
			for i in xrange(self.num_chunks):
				my_print("Processing chunk %d..." % i)
				cur_jobs = set(self.application.status.getActiveJobsIds())
				my_print("Current jobs: %s" % cur_jobs)
				result = call_in_background(run_fulldataset, *(self.application.sc, self.fname, data[fulldataset_chunk_size*i:fulldataset_chunk_size*(i+1)], self.nrows, self.ncols))
				self.spark_job_id = -1
				while self.spark_job_id == -1:
					yield async_sleep(1)
					my_print("Current jobs: %s" % set(self.application.status.getActiveJobsIds()))
					if len(set(self.application.status.getActiveJobsIds()) - cur_jobs) > 0:
						self.spark_job_id = list(set(self.application.status.getActiveJobsIds()) - cur_jobs)[0]
				## if this job hasn't started yet, add it
				if self.job_id == -1:
					self.job_id = self.application.add_job(self.spark_job_id, self.formula_id, self.dataset_id, self.job_type, datetime.now())
				else:
					self.application.jobs[self.job_id]["spark_id"] = self.spark_job_id
				while result.empty():
					yield async_sleep(1)
				self.process_res_fulldataset(result, offset=fulldataset_chunk_size*i)
		else:
			my_print("[ERROR] Incorrect run query %s!" % query_id)
			return

