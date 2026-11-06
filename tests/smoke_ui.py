"""Runs app.py end-to-end against a fake Streamlit + Plotly (no browser needed).
Checks the data flow through all 8 tabs; it does NOT check visual rendering.  Run: python tests/smoke_ui.py"""
import sys, types, datetime
from unittest.mock import MagicMock
class SS(dict):
    __getattr__=dict.get
    def __setattr__(s,k,v): s[k]=v
st=MagicMock(name="st")
class Ctx:
    def __enter__(s): return s
    def __exit__(s,*a): return False
    def __getattr__(s,name): return getattr(st,name)
st.session_state=SS()
st.sidebar=Ctx()
st.set_page_config=lambda **k:None
st.cache_data=lambda *a,**k:(lambda f:f)
st.file_uploader=lambda *a,**k:None
st.checkbox=lambda label,value=False,**k:value
st.date_input=lambda *a,value=None,**k:value
st.selectbox=lambda label,options,index=0,format_func=None,**k:(list(options)[index] if len(list(options)) else None)
st.text_input=lambda label,value="",**k:value
st.text_area=lambda label,value="",**k:value
st.multiselect=lambda label,options,default=None,**k:default or []
st.radio=lambda label,options,**k:options[0]
st.slider=lambda label,a,b,v,**k:v
st.button=lambda *a,**k:False
st.chat_input=lambda *a,**k:None
st.data_editor=lambda df,**k:df
st.columns=lambda n,**k:[Ctx() for _ in range(n if isinstance(n,int) else len(n))]
st.tabs=lambda names:[Ctx() for _ in names]
st.expander=lambda *a,**k:Ctx()
st.chat_message=lambda *a,**k:Ctx()
st.spinner=lambda *a,**k:Ctx()
st.stop=lambda:(_ for _ in ()).throw(SystemExit("st.stop"))
st.column_config=MagicMock()
sys.modules['streamlit']=st
for m in ['plotly','plotly.express','plotly.graph_objects']: sys.modules[m]=MagicMock()
import pathlib; ROOT=pathlib.Path(__file__).resolve().parent.parent; sys.path.insert(0,str(ROOT)); sys.argv=['app.py']
src=open(__import__('pathlib').Path(__file__).resolve().parent.parent / 'app.py').read()
import os; os.chdir(ROOT); exec(compile(src,'app.py','exec'),{'__name__':'__main__','__file__':str(ROOT/'app.py')})
print("APP RAN OK; downloads:",len(st.download_button.call_args_list),"| metrics:",len(st.metric.call_args_list))
