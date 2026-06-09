class HyperObject:

    def to_record(self):
        return self.__dict__

    def from_record(self, record_dict):
        self.__dict__.update(record_dict)
        return self
