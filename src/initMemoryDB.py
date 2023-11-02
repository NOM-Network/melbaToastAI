import memoryDB
from typing import List

memDB = memoryDB.MemoryDB(path="db")

def initSysPrompts(filePath: str):
    systemPrompts: List[str] = []
    curPrompt = ""

    with open(filePath) as sysPromptFile:
        for line in sysPromptFile.readlines():
            if line.find("-=sysPromptSplitter=-") != -1:
                systemPrompts.append(curPrompt.replace("\n", ""))
                curPrompt = ""
            else:
                curPrompt += line
        if curPrompt != '':
            systemPrompts.append(curPrompt.replace("\n", ""))

    for prompt in systemPrompts:
        memDB.newDBEntry(type="systemPrompt", identifier="generic", content=prompt)

def initCharacterMemory(filePath: str):
    characterInformation: List[str] = []
    characterNames: List[str] = []
    curInformation = ""
    curCharacter = ""

    with open(filePath) as charInformationFile:
        for line in charInformationFile.readlines():
            if line.find("-=charInfoSplitter=-") != -1:
                characterInformation.append(curInformation.replace("\n", ""))
                characterNames.append(curCharacter.replace("\n", ""))
                curInformation = ""
                curCharacter = ""
            elif line.find("-=charInfoStart=-") != -1:
                curCharacter = line[17:]
            else:
                curInformation += line
        if curInformation != '':
            characterInformation.append(curInformation.replace("\n", ""))
            characterNames.append(curCharacter.replace("\n", ""))
    for name in characterNames:
        memDB.newDBEntry(type="characterinformation", identifier=name,
                         content=characterInformation[characterNames.index(name)])

def initSwearWords(filePath: str, filePathExclusions: str = None):
    with open(filePath) as swearWords:
        swearWordsFull = swearWords.read()
    if filePathExclusions is not None:
        with open(filePathExclusions):
            for line in swearWords.readlines():
                swearWordsFull = swearWordsFull.replace(line, "")
    print(swearWordsFull)
    if swearWordsFull != "":
        memDB.newDBEntry(type="swearwords", identifier="all", content=swearWordsFull)


initSysPrompts(filePath="memories/systemPrompts.txt")
initCharacterMemory(filePath="memories/characterInformation.txt")
initSwearWords(filePath="memories/bannedWords.txt")