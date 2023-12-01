import ctypes

import LLMUtils
from llama_cpp import llama_cpp
from llama_cpp import llava_cpp
import numpy as np
import numpy.typing as npt
from typing import List
from time import time
from random import randint
from urllib import request
import array

class LlamaModel:
    def __init__(self, parameters: LLMUtils.LLMConfig):
        self.parameters = parameters
        self.threadCount = self.parameters.threads
        if self.threadCount < 6:
            print("Low thread count. Inference might be slow.")

        self.ctxParams = self.parameters.getCtxParms()
        self.ctxParams.n_ctx = self.parameters.nCtx  # default
        self.ctxParams.n_threads = self.threadCount
        if self.ctxParams.seed <= 0:
            self.ctxParams.seed = int(randint(0, int(time())))

        self.modelParams = self.parameters.getModelParams()
        self.modelParams.n_gpu_layers = self.parameters.nOffloadLayer
        self.modelParams.main_gpu = self.parameters.mainGPU
        self.modelPath = self.parameters.modelPath

        if self.modelPath is not None:
            try:
                self.model = llama_cpp.llama_load_model_from_file(self.modelPath.encode('utf-8'), self.modelParams)
            except FileExistsError:
                print("Invalid filepath for model")
        else:
            self.warnAndExit(function="__init__", errorMessage="No model path found")

        if self.model:
            self.context = llama_cpp.llama_new_context_with_model(self.model, self.ctxParams)

        self.n_ctx = llama_cpp.llama_n_ctx(self.context)
        self.nVocab = llama_cpp.llama_n_vocab(self.model) if self.model else self.warnAndExit("__init__",
                                                                                              "No model was found")
        self.nCtx = llama_cpp.llama_n_ctx(self.context) if self.context else self.warnAndExit("__init__",
                                                                                              "No context was found")
        self._init()

    def _init(self):
        self.pCandidatesData = np.array(
            [],
            dtype=np.dtype(
                [("id", np.intc), ("logit", np.single), ("p", np.single)],
                align=True
            ),
        )

        self.pCandidatesData.resize(3, self.nVocab, refcheck=False)
        candidates = llama_cpp.llama_token_data_array(
            data=self.pCandidatesData.ctypes.data_as(llama_cpp.llama_token_data_p),
            size=self.nVocab,
            sorted=False
        )

        self.pCandidates = candidates
        self.EOSToken = 32000# llama_cpp.llama_token_eos(self.context)
        self.pCandidatesDataId = np.arange(self.nVocab, dtype=np.intc)
        self.pCandidatesDataP = np.zeros(self.nVocab, dtype=np.single)
        self.pastTokens = 0

        self.inputIds: npt.NDArray[np.intc] = np.ndarray((self.nCtx,), dtype=np.intc)
        self.scores: npt.NDArray[np.single] = np.ndarray((self.nCtx, self.nVocab), dtype=np.single)

    def reset(self):
        self._init()

    def update(self, newParameters: LLMUtils.LLMConfig):
        self.parameters = newParameters
        self.nCtx = self.parameters.nCtx

    def warnAndExit(self, function, errorMessage):
        raise RuntimeError(f"LLMCore: Error in function: '{function}'. Following error message was provided: '{errorMessage}'\n")

    def tokenizeFull(self, input: str, bos: bool = False, special: bool = True) -> List[int]:   # Possibly further abstract by adding single
        tokens = (llama_cpp.llama_token * self.nCtx)()              # token, tokenization
        newTokens = llama_cpp.llama_tokenize(model=self.model,
                                             text=input.encode("utf8"),
                                             text_len=len(input.encode("utf8")),
                                             tokens=tokens,
                                             n_max_tokens=self.nCtx,
                                             add_bos=bos,
                                             special=special)
        return list(tokens[:newTokens])

    def evaluate(self, tokens: List[int], batch: int = 1):
        if batch > 0:
            for i in range(0, len(tokens), batch):
                nBatch = tokens[i : min(len(tokens), i + batch)]
                nPast = min(self.nCtx - len(nBatch), len(self.inputIds[: self.pastTokens]))
                nTokens = len(nBatch)
                evalCode = llama_cpp.llama_eval(ctx=self.context,
                                                tokens=(llama_cpp.llama_token * len(nBatch))(*nBatch),
                                                n_tokens=nTokens,
                                                n_past=nPast,)

                if evalCode != 0:
                    self.warnAndExit("evaluate", f"Error occured during evaluation of {len(tokens)} tokens with batch"
                                                 f"size {batch}")

                self.inputIds[self.pastTokens : self.pastTokens + nTokens] = nBatch
                rows = nTokens if self.parameters.logitsAll else 1
                cols = self.nVocab
                offset = (0 if self.parameters.logitsAll else nTokens-1)
                self.scores[self.pastTokens+offset:self.pastTokens+nTokens, :].reshape(-1)[:] \
                    = llama_cpp.llama_get_logits(self.context)[: rows * cols]
                self.pastTokens += nTokens

    def sampleTokenWithModel(self):
        lastNTokensSize = self.parameters.n_keep if self.parameters.n_keep != -1 else self.nCtx
        lastNTokensData = [llama_cpp.llama_token(0)] * max(
            0, self.parameters.n_keep - len(self.inputIds[: self.pastTokens])
        ) + self.inputIds[: self.pastTokens][-lastNTokensSize :].tolist()

        lastNTokensData = (llama_cpp.llama_token * lastNTokensSize)(*lastNTokensData)

        logits: npt.NDArray[np.single] = self.scores[: self.pastTokens, :][-1, :]
        for token, bias in self.parameters.logit_bias.items():
            logits[token] = logits[token] * bias
        candidates = self.pCandidates
        candidatesData = self.pCandidatesData
        candidatesData["id"][:] = self.pCandidatesDataId
        candidatesData["logit"][:] = logits
        candidatesData["p"][:] = self.pCandidatesDataP
        candidates.data = candidatesData.ctypes.data_as(llama_cpp.llama_token_data_p)
        candidates.sorted = llama_cpp.c_bool(False)
        candidates.size = llama_cpp.c_size_t(self.nVocab)

        # actually sample the token
        llama_cpp.llama_sample_repetition_penalties(ctx=self.context,
                                                    candidates=llama_cpp.ctypes.byref(candidates),
                                                    last_tokens_data=lastNTokensData,
                                                    penalty_last_n=lastNTokensSize,
                                                    penalty_freq=self.parameters.frequency_penalty,
                                                    penalty_repeat=self.parameters.repeat_penalty,
                                                    penalty_present=self.parameters.presence_penalty)

        if self.parameters.temperature == 0.0:
            id = llama_cpp.llama_sample_token_greedy(ctx=self.context,
                                                     candidates=llama_cpp.ctypes.byref(candidates))
        elif self.parameters.mirostat == 1:
            mirostatMU = llama_cpp.c_float(2.0*self.parameters.mirostat_tau)
            mirostatM = llama_cpp.c_int(100)
            llama_cpp.llama_sample_temperature(ctx=self.context,
                                               candidates=llama_cpp.ctypes.byref(candidates),
                                               temp=self.parameters.temperature)
            id = llama_cpp.llama_sample_token_mirostat(ctx=self.context,
                                                       candidates=llama_cpp.ctypes.byref(candidates),
                                                       tau=self.parameters.mirostat_tau,
                                                       eta=self.parameters.mirostat_eta,
                                                       mu=llama_cpp.ctypes.byref(mirostatMU),
                                                       m=mirostatM)
        elif self.parameters.mirostat == 2:
            mirostatMU = llama_cpp.c_float(2.0*self.parameters.mirostat_tau)
            llama_cpp.llama_sample_temperature(ctx=self.context,
                                               candidates=llama_cpp.ctypes.byref(candidates),
                                               temp=self.parameters.temperature)
            id = llama_cpp.llama_sample_token_mirostat_v2(ctx=self.context,
                                                          candidates=llama_cpp.ctypes.byref(candidates),
                                                          tau=self.parameters.mirostat_tau,
                                                          eta=self.parameters.mirostat_eta,
                                                          mu=llama_cpp.ctypes.byref(mirostatMU))
        else:   # temperature sampling
            llama_cpp.llama_sample_top_k(ctx=self.context,
                                         candidates=llama_cpp.ctypes.byref(candidates),
                                         k=self.parameters.top_k,
                                         min_keep=llama_cpp.c_size_t(1))
            llama_cpp.llama_sample_tail_free(ctx=self.context,
                                             candidates=llama_cpp.ctypes.byref(candidates),
                                             z=self.parameters.tfs_z,
                                             min_keep=llama_cpp.c_size_t(1))
            llama_cpp.llama_sample_typical(ctx=self.context,
                                           candidates=llama_cpp.ctypes.byref(candidates),
                                           p=llama_cpp.c_float(1.0),
                                           min_keep=llama_cpp.c_size_t(1))
            llama_cpp.llama_sample_top_p(ctx=self.context,
                                         candidates=llama_cpp.ctypes.byref(candidates),
                                         p=self.parameters.top_p,
                                         min_keep=llama_cpp.c_size_t(1))
            llama_cpp.llama_sample_temperature(ctx=self.context,
                                               candidates=llama_cpp.ctypes.byref(candidates),
                                               temp=self.parameters.temperature)
            id = llama_cpp.llama_sample_token(ctx=self.context,
                                              candidates=llama_cpp.ctypes.byref(candidates))
        return id

    def generateTokens(self, tokens: List[int]):
        nTokens = 0
        while True:
            self.evaluate(tokens=tokens, batch=64)
            newToken = self.sampleTokenWithModel()
            tokensON = yield newToken
            tokens = [newToken]
            if tokensON:
                tokens.extend(tokensON)

            nTokens += 1

    def tokenToByte(self, token: int) -> bytes:
        size = 32
        buffer = (llama_cpp.ctypes.c_char * size)()
        n = llama_cpp.llama_token_to_piece(self.model, llama_cpp.llama_token(token), buffer, size)

        assert n <= size
        return bytes(buffer[:n])  # no llama1 support

    def tokensToString(self, tokens: List[int]) -> str:
        buf = b""
        for token in tokens:
            buf += self.tokenToByte(token=token)
        return buf.decode("utf8", errors="ignore")

    def generate(self, stream: bool = False) -> str:   # streaming disabled for now
        antiPrompts: List[str] = self.parameters.antiPrompt
        exitTokens: List[int] = [32002, 32001]
        tempBytes = b""
        finalString = ""
        tokens: List[int] = []
        tokenizedPromptTokens: List[int] = (self.tokenizeFull(self.parameters.prompt) if self.parameters.prompt != ""
                                            else [llama_cpp.llama_token_bos(self.context)])
        
        if len(tokenizedPromptTokens) >= self.parameters.nCtx:
            print(f"{tokenizedPromptTokens} tokens were requested to be processed, maximum is "
                  f"{llama_cpp.llama_n_ctx(self.context)}")
            return ""

        llama_cpp.llama_reset_timings(self.context)
        if antiPrompts != []:
            encodedAntiPrompts: List[bytes] = [a.encode("utf8") for a in antiPrompts]
        else:
            encodedAntiPrompts: List[bytes] = []

        incompleteFix: int = 0
        for t in self.generateTokens(tokens=tokenizedPromptTokens):  # should probably remove either tempbytes or
            if t == self.EOSToken or t in exitTokens:                # finalstring
                finalString = self.tokensToString(tokens=tokens) if len(finalString)+1 != len(tokens) else finalString
                break
            tokens.append(t)
            tempBytes += self.tokenToByte(token=tokens[-1])

            for k, char in enumerate(tempBytes[-3:]):
                k = 3 - k
                for number, pattern in [(2, 192), (3, 224), (4, 240)]:
                    if number > k and pattern & char == pattern:
                        incompleteFix = number - k

            if incompleteFix > 0:
                incompleteFix -= 1
                continue

            antiPrompt = [a for a in encodedAntiPrompts if a in tempBytes]
            if len(antiPrompt) > 0:
                firstAntiPrompt = antiPrompt[0]
                tempBytes = tempBytes[: tempBytes.index(firstAntiPrompt)]
                break

            # implement streaming

            if len(tokens) > self.parameters.n_predict:
                finalString = self.tokensToString(tokens=tokens)
                break

        llama_cpp.llama_print_timings(self.context)
        return finalString if finalString != "" else tempBytes.decode("utf-8", errors="ignore")

    def response(self, stream: bool = False) -> str:    # streaming disabled for now
        if not stream:
            return self.generate(stream=stream)
        else:
            return "placeholder"

    def loadPrompt(self, path: str = None, prompt: str = None, type: str = None):
        supportedPromptTypes = ['alpaca', 'pygmalion', 'pygmalion2', 'zephyr-beta', 'openhermes-mistral']

        if path is not None:
            with open(path) as f:
                self.parameters.prompt = (" " + (f.read()).replace("{llmName}", self.parameters.modelName))
                self.parameters.prompt.replace("\\n", '\n')

            if type.lower() not in supportedPromptTypes:
                print(f"Prompt type not supported. Prompt type: {type.lower()}")
                self.global_go = False
                pass
        elif prompt is not None:
            self.parameters.prompt = prompt
        else:
            print("LLMCore: No prompt loaded.")

        self._promptTemplate = ""
        if type.lower() == "alpaca":
            self.systemPromptPrefix = ""
            self.inputPrefix = "### Instruction:"
            self.outputPrefix = "### Response:"
        elif type.lower() == "pygmalion":
            self.systemPromptPrefix = "{llmName}}'s Persona:"
            self.systemPromptSplitter = "<START>"
            self.inputPrefix = "You:"
            self.outputPrefix = ('[' + self.parameters.modelName + ']' + ':' + ' ')
            self.parameters.prompt.replace("PYGMALION", " ")
        elif type.lower() == "pygmalion2":
            self.systemPromptPrefix = ""
            self.inputPrefix = "<|user|>"
            self.outputPrefix = "<|model|>"
            self.parameters.prompt.replace("PYGMALION2", "")
        elif type.lower() == "openchat-3.5":
            self.userInputPrefix = "GPT4 User"
            self.llmOutputPrefix = "GPT4 Assistant"
            self.inputSuffix = "<|end_of_turn|>"
            self.inputPrefix = "<s>"
            self.systemPromptPrefix = "GPT4 User"
            self.systemPromptSplitter = "<|end_of_turn|>"
            self._promptTemplate = f"{self.userInputPrefix}:\n[inputText]{self.inputSuffix}\n" \
                                   f"{self.llmOutputPrefix}: "
        elif type.lower() == "zephyr-beta":
            self.systemPromptPrefix = "<|system|>"
            self.systemPromptSplitter = "</s>"
            self.userInputPrefix = "<|user|>"
            self.llmOutputPrefix = "<|assistant|>"
            self.inputSuffix = "</s>"
            self._promptTemplate = f"{self.userInputPrefix}\n[inputText]{self.inputSuffix}\n" \
                                   f"{self.llmOutputPrefix}\n"
        elif type.lower() == "openhermes-mistral":
            self.systemPromptSplitter = "<|im_end|>"
            self.systemPromptPrefix = "<|im_start|>system"
            self.inputPrefix = "<|im_start|>"
            self.inputSuffix = "<|im_end|>"
            self._promptTemplate = f"{self.inputPrefix}user\n[inputText]{self.inputSuffix}\n" \
                                  f"{self.inputPrefix}assistant\n"

    def promptTemplate(self, inputText: str):
        prompt = self._promptTemplate.replace("[inputText]", inputText)
        return prompt

    def printPrompt(self):
        if self.parameters.prompt:
            print(self.parameters.prompt)

    def exit(self):
        llama_cpp.llama_print_timings(self.context)
        llama_cpp.llama_free(self.context)

class LlamaLlavaModel:
    def __init__(self, llamaModel = None, modelPath: str = None, parameters: LLMUtils.LLMConfig = None):
        if llamaModel is None:
            self.warnAndExit(function="__init__", errorMessage="No Llama model provided.")
        if modelPath is None:
            self.warnAndExit(function="__init__", errorMessage="No model path provided.")

        if parameters is None:
            self.parameters = LLMUtils.LLMConfig()
        else:
            self.parameters = parameters

        self.model = llamaModel
        self.context = llava_cpp.clip_model_load(fname=modelPath.encode(), verbosity=1)
        self.image = None

    def getImage(self, image: str):
        imagedata = request.urlopen(url=image).read()
        if imagedata != self.image:
            self.image = imagedata

    def embedImage(self, imageurl: str):
        if imageurl is None and self.image is None:
            self.warnAndExit(function="embedImage", errorMessage="No image URL provided.")
        self.getImage(image=imageurl)

        data = array.array("B", self.image)
        dataptr = (ctypes.c_ubyte * len(data)).from_buffer(data)
        self.embedding = llava_cpp.llava_image_embed_make_with_bytes(ctx_clip=self.context,
                                                                n_threads=self.parameters.threads,
                                                                image_bytes=dataptr,
                                                                image_bytes_length=len(self.image))

    def evalEmbedding(self):
        nPast = ctypes.c_int(self.model.pastTokens)
        nPastPtr = ctypes.pointer(nPast)

        llava_cpp.llava_eval_image_embed(ctx_llama=self.model.context,
                                         embed=self.embedding,
                                         n_batch=32,
                                         n_past=nPastPtr)
        self.model.pastTokens = nPast.value

        llava_cpp.llava_image_embed_free(embed=self.embedding)

    def response(self, systemprompt: str = None, prompt: str = None, imageurl: str = None):
        defaultsystemprompt = "A chat between a helpful assistant that thinks logically and a user.\n"
        defaultuserprompt = "USER: Give a brief explanation for this image"
        promptTokens = self.model.tokenizeFull(input=systemprompt,
                                               bos=True,
                                               special=False)
        self.model.evaluate(tokens=promptTokens, batch=self.parameters.n_batch)

        self.embedImage(imageurl=imageurl)
        self.evalEmbedding()

        inputTokens = self.model.tokenizeFull(input=prompt,
                                              bos=False,
                                              special=False)
        self.model.evaluate(tokens=inputTokens, batch=self.parameters.n_batch)

        exitTokens: List[int] = [2, 32002, 32001]
        generatedTokens: List[int] = []
        tempBytes = b""
        tokens = []

        # llama_cpp.llama_reset_timings(self.context) broken

        incompleteFix: int = 0
        for t in self.model.generateTokens(tokens=tokens):
            if t == self.model.EOSToken or t in exitTokens:
                print(t)
                break

            tokens.append(t)
            generatedTokens.append(t)
            tempBytes += self.model.tokenToByte(token=generatedTokens[-1])

            for k, char in enumerate(tempBytes[-3:]):
                k = 3 - k
                for number, pattern in [(2, 192), (3, 224), (4, 240)]:
                    if number > k and pattern & char == pattern:
                        incompleteFix = number - k

            if incompleteFix > 0:
                incompleteFix -= 1
                continue

            if len(generatedTokens) > self.parameters.n_predict:
                break

        finalString = self.model.tokensToString(tokens=generatedTokens)
        # llama_cpp.llama_print_timings(self.context) broken
        print(f"Final {generatedTokens} - Tokens {tokens} - tempBytes {tempBytes}")
        return finalString if finalString != "" else tempBytes.decode("utf-8", errors="ignore")


    def warnAndExit(self, function, errorMessage):
        raise RuntimeError(f"LLMCore: Error in function: '{function}'. Following error message was provided: '{errorMessage}'\n")