# -*- coding: utf-8 -*-
import os, sys, json, time, logging, importlib.util, traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Any
os.environ['http_proxy'] = ''
os.environ['https_proxy'] = ''
os.environ['HTTP_PROXY'] = ''
os.environ['HTTPS_PROXY'] = ''
os.environ['ALL_PROXY'] = ''
os.environ['all_proxy'] = ''
import akshare as ak, numpy as np, pandas as pd, requests, urllib.request, urllib.error
