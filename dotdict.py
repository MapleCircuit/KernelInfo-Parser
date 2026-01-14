class DotDict(dict):
    def __init__(self, *args, **kwargs):
        
        if type(args[0]) is list:
            largs=[]
            largs.append(*args)
            d=dict(zip(largs[0],largs[0]))
            largs[0]=d.copy()
            super().__init__(*largs, **kwargs)
        else:
            super().__init__(*args, **kwargs)
        for thekey, thevalue in self.items():
            if isinstance(thevalue, dict):
                self[thekey] = DotDict(thevalue)

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as e:
            raise AttributeError(key) from e

    def __setattr__(self, key, value):
        self[key] = value

    def to_dict(self):
        result = {}
        for thekey, thevalue in self.items():
            if isinstance(thevalue, DotDict):
                result[thekey] = thevalue.to_dict()
            else:
                result[thekey] = thevalue
        return result



    def _getPart(self,allkeylevels):
        # split with dots.
        # for each part, 
        # check if []
        
        token = allkeylevels[0]
        keyname = token
        keyindex = None
        if '[' in token: 
            subtokens=token.partition("[") # token[index]-> token,'[,index]  
            keyindex=subtokens[2].partition("]")[0] # index] -> index,],None
            keyname = subtokens[0]
        
        k = allkeylevels[1:]
        return keyname,keyindex,k



    def getValueFQN( self, key, default=None ):
        
        k=key.split('.')
        v=dict(self.items())
        r=None
        while k:
            keyname,keyindex,k = self._getPart(k)
            if keyindex:
                if keyindex == '#':
                    r=len(v[keyname])
                    k=[]
                elif keyname == 'Top':
                    r=list(v.values())[int(keyindex)]
                    k=None
                else:
                    v=v[keyname][int(keyindex)]
            else:
                v=v[keyname]
        if r : 
            return r
        return v


