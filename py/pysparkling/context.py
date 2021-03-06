from pyspark.context import SparkContext
from pyspark.sql.dataframe import DataFrame
from pyspark.rdd import RDD
from pyspark.sql import SparkSession
from h2o.frame import H2OFrame
from pysparkling.initializer import Initializer
from pysparkling.conf import H2OConf
import h2o
from pysparkling.conversions import FrameConversions as fc
import warnings
import atexit
import sys

def _monkey_patch_H2OFrame(hc):
    @staticmethod
    def determine_java_vec_type(vec):
        if vec.isCategorical():
            return "enum"
        elif vec.isUUID():
            return "uuid"
        elif vec.isString():
            return "string"
        elif vec.isInt():
            if vec.isTime():
                return "time"
            else:
                return "int"
        else:
            return "real"

    def get_java_h2o_frame(self):
        # Can we use cached H2O frame?
        # Only if we cached it before and cache was not invalidated by rapids expression
        if not hasattr(self, '_java_frame') or self._java_frame is None \
           or self._ex._cache._id is None or self._ex._cache.is_empty() \
           or not self._ex._cache._id == self._java_frame_sid:
            # Note: self.frame_id will trigger frame evaluation
            self._java_frame = hc._jhc.asH2OFrame(self.frame_id)
        return self._java_frame

    @staticmethod
    def from_java_h2o_frame(h2o_frame, h2o_frame_id, cols_limit=100):
        # Cache Java reference to the backend frame
        sid = h2o_frame_id.toString()
        cols = cols_limit if h2o_frame.numCols() > cols_limit else -1
        fr = H2OFrame.get_frame(sid, cols=cols, light=True)
        fr._java_frame = h2o_frame
        fr._java_frame_sid = sid
        fr._backed_by_java_obj = True
        return fr
    H2OFrame.determine_java_vec_type = determine_java_vec_type
    H2OFrame.from_java_h2o_frame = from_java_h2o_frame
    H2OFrame.get_java_h2o_frame = get_java_h2o_frame

def _is_of_simple_type(rdd):
    if not isinstance(rdd, RDD):
        raise ValueError('rdd is not of type pyspark.rdd.RDD')

    # Python 3.6 does not contain type long
    # this code ensures we are compatible with both, python 2.7 and python 3.6
    if sys.version_info > (3,):
        type_checks = (str, int, bool, float)
    else:
        type_checks = (str, int, bool, long, float)

    if isinstance(rdd.first(), type_checks):
        return True
    else:
        return False

def _get_first(rdd):
    if rdd.isEmpty():
        raise ValueError('rdd is empty')

    return rdd.first()


class H2OContext(object):

    def __init__(self, spark_session):
        """
         This constructor is used just to initialize the environment. It does not start H2OContext.
         To start H2OContext use one of the getOrCreate methods. This constructor is internally used in those methods
        """
        try:
            self.__do_init(spark_session)
            _monkey_patch_H2OFrame(self)
            # Load sparkling water jar only if it hasn't been already loaded
            Initializer.load_sparkling_jar(self._sc)
        except:
            raise

    def __do_init(self, spark_session):
        self._spark_session = spark_session
        self._sc = self._spark_session._sc
        self._sql_context = self._spark_session._wrapped
        self._jsql_context = self._spark_session._jwrapped
        self._jspark_session = self._spark_session._jsparkSession
        self._jvm = self._spark_session._jvm

        self.is_initialized = False

    @staticmethod
    def getOrCreate(spark, conf=None, verbose=True, **kwargs):
        """
         Get existing or create new H2OContext based on provided H2O configuration. If the conf parameter is set then
         configuration from it is used. Otherwise the configuration properties passed to Sparkling Water are used.
         If the values are not found the default values are used in most of the cases. The default cluster mode
         is internal, ie. spark.ext.h2o.external.cluster.mode=false

         param - Spark Context or Spark Session
         returns H2O Context
        """

        spark_session = spark
        if isinstance(spark, SparkContext):
            warnings.warn("Method H2OContext.getOrCreate with argument of type SparkContext is deprecated and " +
                          "parameter of type SparkSession is preferred.")
            spark_session = SparkSession.builder.getOrCreate()

        h2o_context = H2OContext(spark_session)

        jvm = h2o_context._jvm  # JVM
        jspark_session = h2o_context._jspark_session  # Java Spark Session


        if conf is not None:
            selected_conf = conf
        else:
            selected_conf = H2OConf(spark_session)
        # Create backing Java H2OContext
        jhc = jvm.org.apache.spark.h2o.JavaH2OContext.getOrCreate(jspark_session, selected_conf._jconf)
        h2o_context._jhc = jhc
        h2o_context._conf = selected_conf
        h2o_context._client_ip = jhc.h2oLocalClientIp()
        h2o_context._client_port = jhc.h2oLocalClientPort()
        # Create H2O REST API client
        h2o.connect(ip=h2o_context._client_ip, port=h2o_context._client_port, verbose=verbose, **kwargs)
        h2o_context.is_initialized = True

        if verbose:
            print(h2o_context)

        # Stop h2o when running standalone pysparkling scripts, only in client deploy mode
        #, so the user does not need explicitly close h2o.
        # In driver mode the application would call exit which is handled by Spark AM as failure
        deploy_mode = spark_session.sparkContext._conf.get("spark.submit.deployMode")
        if deploy_mode != "cluster":
            atexit.register(lambda: h2o_context.__stop())
        return h2o_context

    def __stop(self):
        try:
            h2o.cluster().shutdown()
        except:
            pass

    def stop(self):
        warnings.warn("Stopping H2OContext from PySparkling is not fully supported. Please restart your PySpark session and create a new H2OContext.")

    def __del__(self):
        self.stop()

    def __str__(self):
        if self.is_initialized:
            return self._jhc.toString()
        else:
            return "H2OContext: not initialized, call H2OContext.getOrCreate(spark) or H2OContext.getOrCreate(spark, conf)"

    def __repr__(self):
        self.show()
        return ""

    def show(self):
        print(self)

    def get_conf(self):
        return self._conf

    def as_spark_frame(self, h2o_frame, copy_metadata=True):
        """
        Transforms given H2OFrame to Spark DataFrame

        Parameters
        ----------
          h2o_frame : H2OFrame
          copy_metadata: Bool = True

        Returns
        -------
          Spark DataFrame
        """
        if isinstance(h2o_frame, H2OFrame):
            j_h2o_frame = h2o_frame.get_java_h2o_frame()
            jdf = self._jhc.asDataFrame(j_h2o_frame, copy_metadata, self._jsql_context)
            df = DataFrame(jdf, self._sql_context)
            # Attach h2o_frame to dataframe which forces python not to delete the frame when we leave the scope of this
            # method.
            # Without this, after leaving this method python would garbage collect the frame since it's not used
            # anywhere and spark. when executing any action on this dataframe, will fail since the frame
            # would be missing.
            df._h2o_frame = h2o_frame
            return df

    def as_h2o_frame(self, dataframe, framename=None):
        """
        Transforms given Spark RDD or DataFrame to H2OFrame.

        Parameters
        ----------
          dataframe : Spark RDD or DataFrame
          framename : Optional name for resulting H2OFrame

        Returns
        -------
          H2OFrame which contains data of original input Spark data structure
        """
        if isinstance(dataframe, DataFrame):
            return fc._as_h2o_frame_from_dataframe(self, dataframe, framename)
        elif isinstance(dataframe, RDD):
            # First check if the type T in RDD[T] is one of the python "primitive" types
            # String, Boolean, Int and Double (Python Long is converted to java.lang.BigInteger)
            if _is_of_simple_type(dataframe):
                first = _get_first(dataframe)
                # Make this code compatible with python 3.6 and python 2.7
                global long
                if sys.version_info > (3,):
                    long = int

                if isinstance(first, str):
                    return fc._as_h2o_frame_from_RDD_String(self, dataframe, framename)
                elif isinstance(first, bool):
                    return fc._as_h2o_frame_from_RDD_Bool(self, dataframe, framename)
                elif (isinstance(dataframe.min(), int) and isinstance(dataframe.max(), int)) or (isinstance(dataframe.min(), long) and isinstance(dataframe.max(), long)):
                    if dataframe.min() >= self._jvm.Integer.MIN_VALUE and dataframe.max() <= self._jvm.Integer.MAX_VALUE:
                        return fc._as_h2o_frame_from_RDD_Int(self, dataframe, framename)
                    elif dataframe.min() >= self._jvm.Long.MIN_VALUE and dataframe.max() <= self._jvm.Long.MAX_VALUE:
                        return fc._as_h2o_frame_from_RDD_Long(self, dataframe, framename)
                    else:
                        raise ValueError('Numbers in RDD Too Big')
                elif isinstance(first, float):
                    return fc._as_h2o_frame_from_RDD_Float(self, dataframe, framename)
            else:
                return fc._as_h2o_frame_from_complex_type(self, dataframe, framename)

