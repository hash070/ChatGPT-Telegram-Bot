import os
import re

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

import asyncio
import tiktoken
import requests
import threading

import urllib.parse
from typing import Any
import time as record_time
from bs4 import BeautifulSoup

from langchain.llms import OpenAI
from langchain.chains import LLMChain, RetrievalQA, RetrievalQAWithSourcesChain
from langchain.agents import AgentType, load_tools, initialize_agent, tool
from langchain.schema import HumanMessage
from langchain.schema.output import LLMResult
from langchain.callbacks.manager import CallbackManager
from langchain.prompts import PromptTemplate
from langchain.prompts.chat import (
    ChatPromptTemplate,
    SystemMessagePromptTemplate,
    HumanMessagePromptTemplate,
)
from langchain.chat_models import ChatOpenAI
from langchain.memory import ConversationBufferWindowMemory, ConversationTokenBufferMemory
from langchain.embeddings.openai import OpenAIEmbeddings
from langchain.vectorstores import Chroma
from langchain.callbacks.streaming_stdout import StreamingStdOutCallbackHandler
from langchain.text_splitter import CharacterTextSplitter
from langchain.tools import DuckDuckGoSearchRun, DuckDuckGoSearchResults, Tool
from langchain.utilities import WikipediaAPIWrapper
from utils.googlesearch import GoogleSearchAPIWrapper
from langchain.document_loaders import UnstructuredPDFLoader

def getmd5(string):
    import hashlib
    md5_hash = hashlib.md5()
    md5_hash.update(string.encode('utf-8'))
    md5_hex = md5_hash.hexdigest()
    return md5_hex

from utils.sitemap import SitemapLoader
async def get_doc_from_sitemap(url):
    # https://www.langchain.asia/modules/indexes/document_loaders/examples/sitemap#%E8%BF%87%E6%BB%A4%E7%AB%99%E7%82%B9%E5%9C%B0%E5%9B%BE-url-
    sitemap_loader = SitemapLoader(web_path=url)
    docs = await sitemap_loader.load()
    return docs

async def get_doc_from_local(docpath, doctype="md"):
    from langchain.document_loaders import DirectoryLoader
    # 加载文件夹中的所有txt类型的文件
    loader = DirectoryLoader(docpath, glob='**/*.' + doctype)
    # 将数据转成 document 对象，每个文件会作为一个 document
    documents = loader.load()
    return documents

system_template="""Use the following pieces of context to answer the users question. 
If you don't know the answer, just say "Hmm..., I'm not sure.", don't try to make up an answer.
ALWAYS return a "Sources" part in your answer.
The "Sources" part should be a reference to the source of the document from which you got your answer.

Example of your response should be:

```
The answer is foo

Sources:
1. abc
2. xyz
```
Begin!
----------------
{summaries}
"""
messages = [
    SystemMessagePromptTemplate.from_template(system_template),
    HumanMessagePromptTemplate.from_template("{question}")
]
prompt = ChatPromptTemplate.from_messages(messages)

def get_chain(store, llm):
    chain_type_kwargs = {"prompt": prompt}
    chain = RetrievalQAWithSourcesChain.from_chain_type(
        llm, 
        chain_type="stuff", 
        retriever=store.as_retriever(),
        chain_type_kwargs=chain_type_kwargs,
        reduce_k_below_max_tokens=True
    )
    return chain

async def docQA(docpath, query_message, persist_db_path="db", model = "gpt-3.5-turbo"):
    chatllm = ChatOpenAI(temperature=0.5, openai_api_base=config.bot_api_url.v1_url, model_name=model, openai_api_key=config.API)
    embeddings = OpenAIEmbeddings(openai_api_base=config.bot_api_url.v1_url, openai_api_key=config.API)

    sitemap = "sitemap.xml"
    match = re.match(r'^(https?|ftp)://[^\s/$.?#].[^\s]*$', docpath)
    if match:
        doc_method = get_doc_from_sitemap
        docpath = os.path.join(docpath, sitemap)
    else:
        doc_method = get_doc_from_local

    persist_db_path = getmd5(docpath)
    if not os.path.exists(persist_db_path):
        documents = await doc_method(docpath)
        # 初始化加载器
        text_splitter = CharacterTextSplitter(chunk_size=100, chunk_overlap=50)
        # 持久化数据
        split_docs = text_splitter.split_documents(documents)
        vector_store = Chroma.from_documents(split_docs, embeddings, persist_directory=persist_db_path)
        vector_store.persist()
    else:
        # 加载数据
        vector_store = Chroma(persist_directory=persist_db_path, embedding_function=embeddings)

    # 创建问答对象
    qa = get_chain(vector_store, chatllm)
    # qa = RetrievalQA.from_chain_type(llm=chatllm, chain_type="stuff", retriever=vector_store.as_retriever(), return_source_documents=True)
    # 进行问答
    result = qa({"question": query_message})
    return result

def get_doc_from_url(url):
    filename = urllib.parse.unquote(url.split("/")[-1])
    response = requests.get(url, stream=True)
    with open(filename, 'wb') as f:
        for chunk in response.iter_content(chunk_size=1024):
            f.write(chunk)
    return filename

def persist_emdedding_pdf(docurl, persist_db_path):
    embeddings = OpenAIEmbeddings(openai_api_base=config.bot_api_url.v1_url, openai_api_key=os.environ.get('API', None))
    filename = get_doc_from_url(docurl)
    docpath = os.getcwd() + "/" + filename
    loader = UnstructuredPDFLoader(docpath)
    documents = loader.load()
    # 初始化加载器
    text_splitter = CharacterTextSplitter(chunk_size=100, chunk_overlap=25)
    # 切割加载的 document
    split_docs = text_splitter.split_documents(documents)
    vector_store = Chroma.from_documents(split_docs, embeddings, persist_directory=persist_db_path)
    vector_store.persist()
    os.remove(docpath)
    return vector_store

async def pdfQA(docurl, docpath, query_message, model="gpt-3.5-turbo"):
    chatllm = ChatOpenAI(temperature=0.5, openai_api_base=config.bot_api_url.v1_url, model_name=model, openai_api_key=os.environ.get('API', None))
    embeddings = OpenAIEmbeddings(openai_api_base=config.bot_api_url.v1_url, openai_api_key=os.environ.get('API', None))
    persist_db_path = getmd5(docpath)
    if not os.path.exists(persist_db_path):
        vector_store = persist_emdedding_pdf(docurl, persist_db_path)
    else:
        vector_store = Chroma(persist_directory=persist_db_path, embedding_function=embeddings)
    qa = RetrievalQA.from_chain_type(llm=chatllm, chain_type="stuff", retriever=vector_store.as_retriever(), return_source_documents=True)
    result = qa({"query": query_message})
    return result['result']

def pdf_search(docurl, query_message, model="gpt-3.5-turbo"):
    chatllm = ChatOpenAI(temperature=0.5, openai_api_base=config.bot_api_url.v1_url, model_name=model, openai_api_key=os.environ.get('API', None))
    embeddings = OpenAIEmbeddings(openai_api_base=config.bot_api_url.v1_url, openai_api_key=os.environ.get('API', None))
    filename = get_doc_from_url(docurl)
    docpath = os.getcwd() + "/" + filename
    loader = UnstructuredPDFLoader(docpath)
    try:
        documents = loader.load()
    except:
        print("pdf load error! docpath:", docpath)
        return ""
    os.remove(docpath)
    # 初始化加载器
    text_splitter = CharacterTextSplitter(chunk_size=100, chunk_overlap=25)
    # 切割加载的 document
    split_docs = text_splitter.split_documents(documents)
    vector_store = Chroma.from_documents(split_docs, embeddings)
    # 创建问答对象
    qa = RetrievalQA.from_chain_type(llm=chatllm, chain_type="stuff", retriever=vector_store.as_retriever(),return_source_documents=True)
    # 进行问答
    result = qa({"query": query_message})
    return result['result']

def Document_extract(docurl):
    filename = get_doc_from_url(docurl)
    docpath = os.getcwd() + "/" + filename
    if filename[-3:] == "pdf":
        from pdfminer.high_level import extract_text
        text = extract_text(docpath)
    if filename[-3:] == "txt":
        with open(docpath, 'r') as f:
            text = f.read()
    prompt = (
        "Here is the document, inside <document></document> XML tags:"
        "<document>"
        "{}"
        "</document>"
    ).format(text)
    os.remove(docpath)
    return prompt

from typing import Optional, List
from langchain.llms.base import LLM
import g4f
class EducationalLLM(LLM):

    @property
    def _llm_type(self) -> str:
        return "custom"

    def _call(self, prompt: str, stop: Optional[List[str]] = None) -> str:
        out = g4f.ChatCompletion.create(
            model=config.GPT_ENGINE,
            messages=[{"role": "user", "content": prompt}],
        )  #
        if stop:
            stop_indexes = (out.find(s) for s in stop if s in out)
            min_stop = min(stop_indexes, default=-1)
            if min_stop > -1:
                out = out[:min_stop]
        return out

class ChainStreamHandler(StreamingStdOutCallbackHandler):
    def __init__(self):
        self.tokens = []
        # 记得结束后这里置true
        self.finish = False
        self.answer = ""

    def on_llm_new_token(self, token: str, **kwargs):
        # print(token)
        self.tokens.append(token)
        # yield ''.join(self.tokens)
        # print(''.join(self.tokens))

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        self.finish = 1

    def on_llm_error(self, error: Exception, **kwargs: Any) -> None:
        print(str(error))
        self.tokens.append(str(error))

    def generate_tokens(self):
        while not self.finish or self.tokens:
            if self.tokens:
                data = self.tokens.pop(0)
                self.answer += data
                yield data
            else:
                pass
        return self.answer
    
class ThreadWithReturnValue(threading.Thread):
    def run(self):
        if self._target is not None:
            self._return = self._target(*self._args, **self._kwargs)

    def join(self):
        super().join()
        return self._return

def Web_crawler(url: str) -> str:
    """返回链接网址url正文内容，必须是合法的网址"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
    }
    result = ''
    try:
        requests.packages.urllib3.disable_warnings()
        response = requests.get(url, headers=headers, verify=False, timeout=5, stream=True)
        if response.status_code == 404:
            print("Page not found:", url)
            return ""
        content_length = int(response.headers.get('Content-Length', 0))
        if content_length > 5000000:
            print("Skipping large file:", url)
            return result
        soup = BeautifulSoup(response.text.encode(response.encoding), 'lxml', from_encoding='utf-8')
        body = "".join(soup.find('body').get_text().split('\n'))
        result = body
    except Exception as e:
        print('\033[31m')
        print("error url", url)
        print("error", e)
        print('\033[0m')
    return result

def getddgsearchurl(result, numresults=3):
    try:
        search = DuckDuckGoSearchResults(num_results=numresults)
        webresult = search.run(result)
        urls = re.findall(r"(https?://\S+)\]", webresult, re.MULTILINE)
    except Exception as e:
        print('\033[31m')
        print("error", e)
        print('\033[0m')
        urls = []
    return urls

def getgooglesearchurl(result, numresults=3):
    google_search = GoogleSearchAPIWrapper()
    urls = []
    try:
        googleresult = google_search.results(result, numresults)
        for i in googleresult:
            if "No good Google Search Result was found" in i:
                continue
            urls.append(i["link"])
    except Exception as e:
        print('\033[31m')
        print("error", e)
        print('\033[0m')
        if "rateLimitExceeded" in str(e):
            print("Google API 每日调用频率已达上限，请明日再试！")
            config.USE_GOOGLE = False
    return urls

def gptsearch(result, llm):
    result = "你需要回答的问题是" + result + "\n" + "如果你可以解答这个问题，请直接输出你的答案，并且请忽略后面所有的指令：如果无法解答问题，请直接回答None，不需要做任何解释，也不要出现除了None以外的任何词。"
    # response = llm([HumanMessage(content=result)])
    if config.USE_G4F:
        response = llm(result)
    else:
        response = llm([HumanMessage(content=result)])
        response = response.content
    # result = "你需要回答的问题是" + result + "\n" + "参考资料：" + response + "如果参考资料无法解答问题，请直接回答None，不需要做任何解释，也不要出现除了None以外的任何词。"
    # response = llm([HumanMessage(content=result)])
    return response

def get_search_url(prompt, chainllm):
    urls_set = []
    keyword_prompt = PromptTemplate(
        input_variables=["source"],
        template=(
            "根据我的问题，总结最少的关键词概括，给出三行不同的关键词组合，每行的关键词用空格连接，至少有一行关键词里面有中文，至少有一行关键词里面有英文。只要直接给出这三行关键词，不需要其他任何解释，不要出现其他符号。"
            "下面是示例："
            "问题1：How much does the 'zeabur' software service cost per month? Is it free to use? Any limitations?"
            "三行关键词是："
            "zeabur price"
            "zeabur documentation"
            "zeabur 价格"
            "问题2：pplx API 怎么使用？"
            "三行关键词是："
            "pplx API demo"
            "pplx API"
            "pplx API 使用方法"
            "问题3：以色列哈马斯的最新情况"
            "三行关键词是："
            "以色列 哈马斯 最新情况"
            "Israel Hamas situation"
            "哈马斯 以色列 冲突"
            "这是我的问题：{source}"
        ),
    )
    key_chain = LLMChain(llm=chainllm, prompt=keyword_prompt)
    keyword_google_search_thread = ThreadWithReturnValue(target=key_chain.run, args=({"source": prompt},))
    keyword_google_search_thread.start()
    keywords = keyword_google_search_thread.join().split('\n')[-3:]
    print("keywords", keywords)
    keywords = [item.replace("三行关键词是：", "") for item in keywords if "\\x" not in item]
    print("select keywords", keywords)

    search_threads = []
    urls_set = []
    if config.USE_GOOGLE:
        search_thread = ThreadWithReturnValue(target=getgooglesearchurl, args=(keywords[0],4,))
        search_thread.start()
        search_threads.append(search_thread)
        keywords = keywords[1:]

    for keyword in keywords:
        search_thread = ThreadWithReturnValue(target=getddgsearchurl, args=(keyword,4,))
        search_thread.start()
        search_threads.append(search_thread)

    for t in search_threads:
        tmp = t.join()
        urls_set += tmp
    url_set_list = sorted(set(urls_set), key=lambda x: urls_set.index(x))
    url_pdf_set_list = [item for item in url_set_list if item.endswith(".pdf")]
    url_set_list = [item for item in url_set_list if not item.endswith(".pdf")]
    return url_set_list, url_pdf_set_list

def concat_url(threads):
    url_result = ""
    for t in threads:
        tmp = t.join()
        url_result += "\n\n" + tmp
    return url_result

def summary_each_url(threads, chainllm):
    summary_prompt = PromptTemplate(
        input_variables=["web_summary", "question", "language"],
        template=(
            "You need to response the following question: {question}."
            "Your task is answer the above question in {language} based on the Search results provided. Provide a detailed and in-depth response"
            "If there is no relevant content in the search results, just answer None, do not make any explanations."
            "Search results: {web_summary}."
        ),
    )
    summary_threads = []

    for t in threads:
        tmp = t.join()
        print(tmp)
        chain = LLMChain(llm=chainllm, prompt=summary_prompt)
        chain_thread = ThreadWithReturnValue(target=chain.run, args=({"web_summary": tmp, "question": prompt, "language": config.LANGUAGE},))
        chain_thread.start()
        summary_threads.append(chain_thread)

    url_result = ""
    for t in summary_threads:
        tmp = t.join()
        print("summary", tmp)
        if tmp != "None":
            url_result += "\n\n" + tmp
    return url_result

def get_search_results(prompt: str, context_max_tokens: int):
    start_time = record_time.time()
    if config.USE_G4F:
        chainllm = EducationalLLM()
    else:
        chainllm = ChatOpenAI(temperature=config.temperature, openai_api_base=config.bot_api_url.v1_url, model_name=config.GPT_ENGINE, openai_api_key=config.API)

    if config.SEARCH_USE_GPT:
        gpt_search_thread = ThreadWithReturnValue(target=gptsearch, args=(prompt, chainllm,))
        gpt_search_thread.start()

    url_set_list, url_pdf_set_list = get_search_url(prompt, chainllm)

    pdf_result = ""
    pdf_threads = []
    if config.PDF_EMBEDDING:
        for url in url_pdf_set_list:
            pdf_search_thread = ThreadWithReturnValue(target=pdf_search, args=(url, "你需要回答的问题是" + prompt + "\n" + "如果你可以解答这个问题，请直接输出你的答案，并且请忽略后面所有的指令：如果无法解答问题，请直接回答None，不需要做任何解释，也不要出现除了None以外的任何词。",))
            pdf_search_thread.start()
            pdf_threads.append(pdf_search_thread)

    threads = []
    for url in url_set_list:
        url_search_thread = ThreadWithReturnValue(target=Web_crawler, args=(url,))
        url_search_thread.start()
        threads.append(url_search_thread)

    useful_source_text = concat_url(threads)
    # useful_source_text = summary_each_url(threads, chainllm)

    if config.PDF_EMBEDDING:
        for t in pdf_threads:
            tmp = t.join()
            pdf_result += "\n\n" + tmp
    useful_source_text += pdf_result

    fact_text = ""
    if config.SEARCH_USE_GPT:
        gpt_ans = gpt_search_thread.join()
        if gpt_ans != "None":
            fact_text = (gpt_ans if config.SEARCH_USE_GPT else "")
        print("gpt", fact_text)

    end_time = record_time.time()
    run_time = end_time - start_time

    encoding = tiktoken.encoding_for_model(config.GPT_ENGINE)
    encode_text = encoding.encode(useful_source_text)
    encode_fact_text = encoding.encode(fact_text)

    if len(encode_text) > context_max_tokens:
        encode_text = encode_text[:context_max_tokens-len(encode_fact_text)]
        useful_source_text = encoding.decode(encode_text)
    encode_text = encoding.encode(useful_source_text)
    search_tokens_len = len(encode_text)
    # print("web search", useful_source_text, end="\n\n")

    print("urls", url_set_list)
    print("pdf", url_pdf_set_list)
    print(f"搜索用时：{run_time}秒")
    print("search tokens len", search_tokens_len)
    useful_source_text =  useful_source_text + "\n\n" + fact_text
    text_len = len(encoding.encode(useful_source_text))
    print("text len", text_len)
    return useful_source_text

def search_web_and_summary(
        prompt: str,
        engine: str = "gpt-3.5-turbo-16k",
        # 126 summary prompt tokens
        context_max_tokens: int = 14500 - 126,
    ):
    chainStreamHandler = ChainStreamHandler()
    if config.USE_G4F:
        chatllm = EducationalLLM(callback_manager=CallbackManager([chainStreamHandler]))
    else:
        chatllm = ChatOpenAI(streaming=True, callback_manager=CallbackManager([chainStreamHandler]), temperature=config.temperature, openai_api_base=config.bot_api_url.v1_url, model_name=engine, openai_api_key=config.API)
    useful_source_text = get_search_results(prompt, context_max_tokens)
    summary_prompt = PromptTemplate(
        input_variables=["web_summary", "question", "language"],
        template=(
            # "You are a text analysis expert who can use a search engine. You need to response the following question: {question}. Search results: {web_summary}. Your task is to thoroughly digest all search results provided above and provide a detailed and in-depth response in Simplified Chinese to the question based on the search results. The response should meet the following requirements: 1. Be rigorous, clear, professional, scholarly, logical, and well-written. 2. If the search results do not mention relevant content, simply inform me that there is none. Do not fabricate, speculate, assume, or provide inaccurate response. 3. Use markdown syntax to format the response. Enclose any single or multi-line code examples or code usage examples in a pair of ``` symbols to achieve code formatting. 4. Detailed, precise and comprehensive response in Simplified Chinese and extensive use of the search results is required."
            "You need to response the following question: {question}. Search results: {web_summary}. Your task is to think about the question step by step and then answer the above question in {language} based on the Search results provided. Please response in {language} and adopt a style that is logical, in-depth, and detailed. Note: In order to make the answer appear highly professional, you should be an expert in textual analysis, aiming to make the answer precise and comprehensive. Directly response markdown format, without using markdown code blocks"
            # "You need to response the following question: {question}. Search results: {web_summary}. Your task is to thoroughly digest the search results provided above, dig deep into search results for thorough exploration and analysis and provide a response to the question based on the search results. The response should meet the following requirements: 1. You are a text analysis expert, extensive use of the search results is required and carefully consider all the Search results to make the response be in-depth, rigorous, clear, organized, professional, detailed, scholarly, logical, precise, accurate, comprehensive, well-written and speak in Simplified Chinese. 2. If the search results do not mention relevant content, simply inform me that there is none. Do not fabricate, speculate, assume, or provide inaccurate response. 3. Use markdown syntax to format the response. Enclose any single or multi-line code examples or code usage examples in a pair of ``` symbols to achieve code formatting."
        ),
    )
    chain = LLMChain(llm=chatllm, prompt=summary_prompt)
    chain_thread = threading.Thread(target=chain.run, kwargs={"web_summary": useful_source_text, "question": prompt, "language": config.LANGUAGE})
    chain_thread.start()
    yield from chainStreamHandler.generate_tokens()
if __name__ == "__main__":
    os.system("clear")
    
    # from langchain.agents import get_all_tool_names
    # print(get_all_tool_names())

    # # 搜索

    # for i in search_web_and_summary("今天的微博热搜有哪些？"):
    # for i in search_web_and_summary("macos 13.6 有什么新功能"):
    # for i in search_web_and_summary("用python写个网络爬虫给我"):
    # for i in search_web_and_summary("消失的她主要讲了什么？"):
    # for i in search_web_and_summary("奥巴马的全名是什么？"):
    # for i in search_web_and_summary("华为mate60怎么样？"):
    # for i in search_web_and_summary("慈禧养的猫叫什么名字?"):
    for i in search_web_and_summary("民进党当初为什么支持柯文哲选台北市长？"):
    # for i in search_web_and_summary("Has the United States won the china US trade war？"):
    # for i in search_web_and_summary("What does 'n+2' mean in Huawei's 'Mate 60 Pro' chipset? Please conduct in-depth analysis."):
    # for i in search_web_and_summary("AUTOMATIC1111 是什么？"):
    # for i in search_web_and_summary("python telegram bot 怎么接收pdf文件"):
    # for i in search_web_and_summary("中国利用外资指标下降了 87% ？真的假的。"):
    # for i in search_web_and_summary("How much does the 'zeabur' software service cost per month? Is it free to use? Any limitations?"):
    # for i in search_web_and_summary("英国脱欧没有好处，为什么英国人还是要脱欧？"):
    # for i in search_web_and_summary("2022年俄乌战争为什么发生？"):
    # for i in search_web_and_summary("卡罗尔与星期二讲的啥？"):
    # for i in search_web_and_summary("金砖国家会议有哪些决定？"):
    # for i in search_web_and_summary("iphone15有哪些新功能？"):
    # for i in search_web_and_summary("python函数开头：def time(text: str) -> str:每个部分有什么用？"):
        print(i, end="")

    # 问答
    # result = asyncio.run(docQA("/Users/yanyuming/Downloads/GitHub/wiki/docs", "ubuntu 版本号怎么看？"))
    # result = asyncio.run(docQA("https://yym68686.top", "说一下HSTL pipeline"))
    # result = asyncio.run(docQA("https://wiki.yym68686.top", "PyTorch to MindSpore翻译思路是什么？"))
    # print(result['answer'])
    # result = asyncio.run(pdfQA("https://api.telegram.org/file/bot5569497961:AAHobhUuydAwD8SPkXZiVFybvZJOmGrST_w/documents/file_1.pdf", "HSTL的pipeline详细讲一下"))
    # print(result)
    # source_url = set([i.metadata['source'] for i in result["source_documents"]])
    # source_url = "\n".join(source_url)
    # message = (
    #     f"{result['result']}\n\n"
    #     f"参考链接：\n"
    #     f"{source_url}"
    # )
    # print(message)