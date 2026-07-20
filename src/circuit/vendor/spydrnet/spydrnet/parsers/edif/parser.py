from functools import reduce
import re

from spydrnet.parsers.edif.tokenizer import EdifTokenizer
from spydrnet.parsers.edif.edif_tokens import *
from spydrnet.ir import Netlist, Library, Definition, Port, Cable, Instance
from spydrnet.plugins import namespace_manager


class EdifParser:
    def parse_construct(self, construct_parser):
        self.expect_begin_construct()
        result = construct_parser()
        self.expect_end_construct()
        return result

    @staticmethod
    def from_filename(filename):
        parser = EdifParser()
        parser.filename = filename
        return parser

    @staticmethod
    def from_file_handle(file_handle):
        parser = EdifParser()
        parser.file_handle = file_handle
        return parser

    def __init__(self):
        self.edif_identifier_namespace = {} # class -> object -> subclass -> identifier -> object
        self.filename = None
        self.file_handle = None
        self.elements = []
        self.tokenizer = None

    def parse(self):
        self.initialize_tokenizer()
        ns_default = namespace_manager.default
        namespace_manager.default = "EDIF"
        self.netlist = self.parse_construct(self.parse_edif)
        namespace_manager.default = ns_default
        self.tokenizer.__del__()

    def initialize_tokenizer(self):
        if self.filename:
            self.tokenizer = EdifTokenizer.from_filename(self.filename)
        elif self.file_handle:
            self.tokenizer = EdifTokenizer.from_stream(self.file_handle)

    def parse_edif(self):
        environment = Netlist()
        self.append_new_element(environment)
        self.expect(EDIF)
        self.parse_nameDef()
        self.parse_header()
        self.parse_body()
        return self.pop_element()

    def parse_header(self):
        self.parse_construct(self.parse_edifVersion)
        self.parse_construct(self.parse_edifLevel)
        self.parse_construct(self.parse_keywordMap)

    def parse_edifVersion(self):
        self.expect(EDIF_VERSION)
        self.prefix_append("edifVersion")
        version_0 = self.parse_integerToken()
        version_1 = self.parse_integerToken()
        version_2 = self.parse_integerToken()
        self.set_attribute((version_0, version_1, version_2))
        self.prefix_pop()

    def parse_edifLevel(self):
        self.expect(EDIF_LEVEL)
        self.prefix_append("edifLevel")
        level = self.parse_integerToken()
        if level != 0:
            self.set_attribute(level)
        self.prefix_pop()

    def parse_keywordMap(self):
        self.expect(KEYWORD_MAP)
        self.prefix_append("keywordMap")
        self.parse_construct(self.parse_keywordLevel)

        while self.begin_construct():
            self.parse_construct(self.parse_comment)
        self.prefix_pop()

    def parse_keywordLevel(self):
        self.expect(KEYWORD_LEVEL)
        self.prefix_append("keywordLevel")
        level = self.parse_integerToken()
        if level != 0:
            self.set_attribute(level)
        self.prefix_pop()

    def parse_body(self):
        has_status = False
        while self.begin_construct():
            if self.construct_is(STATUS):
                has_status = self.check_for_multiples(STATUS, has_status)
                self.parse_status()

            elif self.construct_is(LIBRARY):
                library = self.parse_library()
                environment = self.elements[-1]
                environment.add_library(library)
            elif self.construct_is(EXTERNAL):
                library = self.parse_external()
                environment = self.elements[-1]
                environment.add_library(library)
            elif self.construct_is(DESIGN):
                self.parse_design()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            elif self.construct_is(USER_DATA):
                self.parse_userData()
            else:
                self.expect(
                    "|".join([STATUS, EXTERNAL, LIBRARY, DESIGN, COMMENT, USER_DATA])
                )
            self.expect_end_construct()

    def parse_status(self):
        self.expect(STATUS)
        self.prefix_append("status")
        while self.begin_construct():
            if self.construct_is(WRITTEN):
                self.parse_written()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            elif self.construct_is(USER_DATA):
                self.parse_userData()
            else:
                self.expect("|".join([WRITTEN | COMMENT | USER_DATA]))
            self.expect_end_construct()
        self.prefix_pop()

    def parse_written(self):
        self.expect(WRITTEN)
        self.prefix_append("written")
        self.parse_construct(self.parse_timeStamp)

        has_author = False
        has_program = False
        has_dataOrigin = False
        while self.begin_construct():
            if self.construct_is(AUTHOR):
                has_author = self.check_for_multiples(AUTHOR, has_author)
                self.parse_author()

            elif self.construct_is(PROGRAM):
                has_program = self.check_for_multiples(PROGRAM, has_program)
                self.parse_program()

            elif self.construct_is(DATA_ORIGIN):
                has_dataOrigin = self.check_for_multiples(DATA_ORIGIN, has_dataOrigin)
                self.parse_dataOrigin()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(METAX):
                self.parse_metax()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            elif self.construct_is(USER_DATA):
                self.parse_userData()
            else:
                self.expect(
                    "|".join(
                        [
                            AUTHOR,
                            PROGRAM,
                            DATA_ORIGIN,
                            PROPERTY,
                            METAX,
                            COMMENT,
                            USER_DATA,
                        ]
                    )
                )
            self.expect_end_construct()
        self.prefix_pop()

    def parse_timeStamp(self):
        self.expect(TIME_STAMP)
        self.prefix_append("timeStamp")
        year = self.parse_integerToken()
        month = self.parse_integerToken()
        day = self.parse_integerToken()
        hour = self.parse_integerToken()
        minute = self.parse_integerToken()
        second = self.parse_integerToken()
        self.set_attribute((year, month, day, hour, minute, second))
        self.prefix_pop()

    def parse_author(self):
        self.expect(AUTHOR)
        self.prefix_append("author")
        author = self.parse_stringToken()
        self.set_attribute(author)
        self.prefix_pop()

    def parse_program(self):
        self.expect(PROGRAM)
        self.prefix_append("program")
        program = self.parse_stringToken()
        self.set_attribute(program)

        if self.begin_construct():
            self.expect(VERSION)
            self.prefix_append("version")
            version = self.parse_stringToken()
            self.set_attribute(version)
            self.prefix_pop()
            self.expect_end_construct()
        self.prefix_pop()

    def parse_library(self):
        self.expect(LIBRARY)
        return self.parse_library_like_element()

    def parse_external(self):
        self.expect(EXTERNAL)
        return self.parse_library_like_element(is_external=True)

    def parse_library_like_element(self, is_external=False):
        library = Library()
        if is_external:
            library["EDIF.external"] = True
        self.append_new_element(library)

        self.parse_nameDef()
        self.parse_construct(self.parse_edifLevel)
        self.parse_construct(self.parse_technology)

        has_status = False
        while self.begin_construct():
            if self.construct_is(STATUS):
                has_status = self.check_for_multiples(STATUS, has_status)
                self.parse_status()

            elif self.construct_is(CELL):
                definition = self.parse_cell()
                library = self.elements[-1]
                add_exception = None
                try:
                    library.add_definition(definition)
                except ValueError as e:
                    name = definition.name
                    identifier = definition["EDIF.identifier"]
                    if name != identifier:
                        try:
                            definition.name = identifier
                            library.add_definition(definition)
                        except ValueError:
                            raise e
                    else:
                        add_exception = e
                if add_exception:
                    raise add_exception
            elif self.construct_is(COMMENT):
                self.parse_comment()
            elif self.construct_is(USER_DATA):
                self.parse_userData()
            else:
                self.expect("|".join([STATUS, CELL, COMMENT, USER_DATA]))
            self.expect_end_construct()

        return self.pop_element()

    def parse_technology(self):
        self.expect(TECHNOLOGY)
        self.parse_construct(self.parse_numberDefinition)
        while self.begin_construct():
            if self.construct_is(FIGURE_GROUP):
                self.parse_figureGroup()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            elif self.construct_is(USER_DATA):
                self.parse_userData()
            else:
                self.expect("|".join([FIGURE_GROUP, PROPERTY, COMMENT, USER_DATA]))
            self.expect_end_construct()

    def parse_figureGroup(self):
        """解析 figureGroup 构造，如 figureGroup PIN / JUNCTION / WIRE 等"""
        self.expect(FIGURE_GROUP)
        self.prefix_append("figureGroup")
        # figureGroup 名称 (如 PIN, JUNCTION, WIRE, BUS, PAGEBORDER)
        self.set_attribute(self.parse_identifier())
        while self.begin_construct():
            if self.construct_is(COLOR):
                self.expect(COLOR)
                color = (self.parse_integerToken(), self.parse_integerToken(), self.parse_integerToken())
                self.set_attribute({"color": color})
            elif self.construct_is(CORNER_TYPE):
                self.expect(CORNER_TYPE)
                self.set_attribute({"cornerType": self.parse_identifier()})
            elif self.construct_is(END_TYPE):
                self.expect(END_TYPE)
                self.set_attribute({"endType": self.parse_identifier()})
            elif self.construct_is(TEXT_HEIGHT):
                self.expect(TEXT_HEIGHT)
                self.set_attribute({"textHeight": self.parse_integerToken()})
            elif self.construct_is(PATH_WIDTH):
                self.parse_pathWidth()
            elif self.construct_is(FILL_PATTERN):
                self.parse_fillPattern()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            elif self.construct_is(USER_DATA):
                self.parse_userData()
            else:
                self.expect("|".join([COLOR, CORNER_TYPE, END_TYPE, TEXT_HEIGHT, PATH_WIDTH, FILL_PATTERN, PROPERTY, COMMENT, USER_DATA]))
            self.expect_end_construct()
        self.prefix_pop()

    def parse_fillPattern(self):
        self.expect(FILL_PATTERN)
        self.prefix_append("fillPattern")
        # 解析两个整数参数
        p1 = self.parse_integerToken()
        p2 = self.parse_integerToken()
        booleans = []
        while self.begin_construct():
            if self.construct_is(BOOLEAN):
                booleans.append(self.parse_boolean())
            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            else:
                self.expect("|".join([BOOLEAN, PROPERTY, COMMENT]))
            self.expect_end_construct()
        self.set_attribute({"p1": p1, "p2": p2, "booleans": booleans})
        self.prefix_pop()

    def parse_pathWidth(self):
        self.expect(PATH_WIDTH)
        self.prefix_append("pathWidth")
        self.set_attribute(self.parse_integerToken())
        self.prefix_pop()

    def parse_numberDefinition(self):
        self.expect(NUMBER_DEFINITION)
        self.prefix_append("numberDefinition")
        while self.begin_construct():
            if self.construct_is(SCALE):
                self.expect(SCALE)
                self.set_attribute({"scale": self.parse_integerToken()})
                while self.begin_construct():
                    if self.construct_is(E):
                        self.expect(E)
                        self.set_attribute({"e": (self.parse_integerToken(), self.parse_integerToken())})
                    elif self.construct_is(UNIT):
                        self.expect(UNIT)
                        self.set_attribute({"unit": self.parse_identifier()})
                    else:
                        self.expect("|".join([E, UNIT]))
                    self.expect_end_construct()
            elif self.construct_is(GRID_MAP):
                self.expect(GRID_MAP)
                self.set_attribute({"gridMap": (self.parse_integerToken(), self.parse_integerToken())})
            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            else:
                self.expect("|".join([SCALE, GRID_MAP, PROPERTY, COMMENT]))
            self.expect_end_construct()
        self.prefix_pop()

    def parse_cell(self):
        definition = Definition()
        self.append_new_element(definition)

        self.expect(CELL)
        self.parse_nameDef()
        self.parse_construct(self.parse_cellType)

        has_status = False
        has_viewMap = False
        while self.begin_construct():
            if self.construct_is(STATUS):
                has_status = self.check_for_multiples(STATUS, has_status)
                self.parse_status()

            elif self.construct_is(VIEW):
                self.parse_view()
            elif self.construct_is(VIEW_MAP):
                has_viewMap = self.check_for_multiples(VIEW_MAP, has_viewMap)
                self.parse_viewMap()

            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            elif self.construct_is(USER_DATA):
                self.parse_userData()
            else:
                self.expect(
                    "|".join([STATUS, VIEW, VIEW_MAP, PROPERTY, COMMENT, USER_DATA])
                )
            self.expect_end_construct()

        return self.pop_element()

    def parse_cellType(self):
        self.expect(CELL_TYPE)
        self.prefix_append("cellType")
        if (
            self.construct_is(GENERIC)
            or self.construct_is(TIE)
            or self.construct_is(RIPPER)
        ):
            if not self.tokenizer.token_equals(GENERIC):
                self.set_attribute(self.tokenizer.token)
        else:
            self.expect("|".join([GENERIC, TIE, RIPPER]))
        self.tokenizer.next()
        self.prefix_pop()

    def parse_view(self):
        self.expect(VIEW)
        self.prefix_append("view")
        self.parse_nameDef()
        self.parse_construct(self.parse_viewType)
        self.parse_construct(self.parse_interface)

        has_status = False
        has_contents = False
        while self.begin_construct():
            if self.construct_is(STATUS):
                has_status = self.check_for_multiples(STATUS, has_status)
                self.parse_status()

            elif self.construct_is(CONTENTS):
                has_contents = self.check_for_multiples(STATUS, has_contents)
                self.parse_contents()

            elif self.construct_is(COMMENT):
                self.parse_comment()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(USER_DATA):
                self.parse_userData()
            else:
                self.expect("|".join([STATUS, CONTENTS, COMMENT, PROPERTY, USER_DATA]))
            self.expect_end_construct()
        self.prefix_pop()

    def parse_viewType(self):
        self.prefix_append("viewType")
        self.expect(VIEW_TYPE)
        if (
            self.construct_is(BEHAVIOR)
            or self.construct_is(DOCUMENT)
            or self.construct_is(GRAPHIC)
            or self.construct_is(LOGICMODEL)
            or self.construct_is(MASKLAYOUT)
            or self.construct_is(NETLIST)
            or self.construct_is(PCBLAYOUT)
            or self.construct_is(SCHEMATIC)
            or self.construct_is(STRANGER)
            or self.construct_is(SYMBOLIC)
        ):
            if not self.tokenizer.token_equals(NETLIST):
                self.set_attribute(self.tokenizer.token)
        else:
            self.expect(
                "|".join(
                    [
                    BEHAVIOR,
                    DOCUMENT,
                    GRAPHIC,
                    LOGICMODEL,
                    MASKLAYOUT,
                    NETLIST,
                    PCBLAYOUT,
                    SCHEMATIC,
                    STRANGER,
                    SYMBOLIC,
                    ]
                )
            )
        self.tokenizer.next()
        self.prefix_pop()

    def parse_interface(self):
        self.expect(INTERFACE)
        has_designator = False
        while self.begin_construct():
            if self.construct_is(PORT):
                port = self.parse_port()
                cell = self.elements[-1]
                cell.add_port(port)
            elif self.construct_is(PORT_BUNDLE):
                self.skip_until_next_construct()
            elif self.construct_is(SYMBOL):
                self.parse_symbol()
            elif self.construct_is(PROTECTION_FRAME):
                self.skip_until_next_construct()
            elif self.construct_is(ARRAY_RELATED_INFO):
                self.skip_until_next_construct()
            elif self.construct_is(PARAMETER):
                self.skip_until_next_construct()
            elif self.construct_is(JOINED):
                self.skip_until_next_construct()  # interface 级别的 joined 不做 pin 连接
            elif self.construct_is(MUST_JOIN):
                self.skip_until_next_construct()
            elif self.construct_is(WEAK_JOINED):
                self.skip_until_next_construct()
            elif self.construct_is(PERMUTABLE):
                self.skip_until_next_construct()
            elif self.construct_is(TIMING):
                self.skip_until_next_construct()
            elif self.construct_is(SIMULATE):
                self.skip_until_next_construct()
            elif self.construct_is(DESIGNATOR):
                has_designator = self.check_for_multiples(DESIGNATOR, has_designator)
                self.parse_designator()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            elif self.construct_is(USER_DATA):
                self.parse_userData()
            else:
                self.expect("|".join([PORT, DESIGNATOR, SYMBOL, JOINED, PROPERTY, COMMENT, USER_DATA]))
            self.expect_end_construct()

    def parse_designator(self):
        self.expect(DESIGNATOR)
        self.prefix_append("designator")
        # designator 可能是直接标识符或包含 stringDisplay 子构造
        if self.begin_construct():
            if self.construct_is(STRING_DISPLAY):
                self.parse_stringDisplay()
            elif self.construct_is(RENAME):
                self.parse_rename()
            self.expect_end_construct()
        elif self.tokenizer.peek_equals(LEFT_PAREN):
            while self.begin_construct():
                if self.construct_is(STRING_DISPLAY):
                    self.parse_stringDisplay()
                elif self.construct_is(PROPERTY):
                    self.parse_property()
                else:
                    self.expect(STRING_DISPLAY)
                self.expect_end_construct()
        else:
            self.set_attribute(self.parse_stringToken())
        self.prefix_pop()

    def parse_port(self):
        self.append_new_element(Port())
        self.expect(PORT)
        if self.begin_construct():
            if self.construct_is(RENAME):
                self.parse_rename()
                port = self.elements[-1]
                port.create_pins(1)

            elif self.construct_is(ARRAY):
                dimension_sizes = self.parse_array()
                pin_count = reduce((lambda x, y: x * y), dimension_sizes)
                port = self.elements[-1]
                port.create_pins(pin_count)
                port.is_array = True
                if "EDIF.original_identifier" in port:
                    # TODO: what about multi-dimensional ports, non-downto ports, and when
                    # non-square brackets are used <0:17><31:0>
                    original_identifier = port["EDIF.original_identifier"]
                    match = re.match(r".*\[(\d+):(\d+)\]", original_identifier)
                    if match:
                        left_index = int(match.group(1))
                        right_index = int(match.group(2))
                        port.lower_index = min(right_index, left_index)

            else:
                self.expect("|".join([RENAME, ARRAY]))
            self.expect_end_construct()
        else:
            self.parse_nameDef()
            port = self.elements[-1]
            port.create_pins(1)
            # TODO: what about single pin array ports with a non_zero starting index.

        has_direction = False
        has_designator = False
        while self.begin_construct():
            if self.construct_is(DIRECTION):
                has_direction = self.check_for_multiples(DIRECTION, has_direction)
                direction = self.parse_direction()
                port = self.elements[-1]
                port.direction = direction

            elif self.construct_is(UNUSED):
                self.skip_until_next_construct()  # 暂未实现，跳过
            elif self.construct_is(DESIGNATOR):
                has_designator = self.check_for_multiples(DESIGNATOR, has_designator)
                self.parse_designator()
            elif self.construct_is(DC_FANIN_LOAD):
                self.skip_until_next_construct()  # 暂未实现，跳过
            elif self.construct_is(DC_FANOUT_LOAD):
                self.skip_until_next_construct()  # 暂未实现，跳过
            elif self.construct_is(DC_MAX_FANIN):
                self.skip_until_next_construct()  # 暂未实现，跳过
            elif self.construct_is(DC_MAX_FANOUT):
                self.skip_until_next_construct()  # 暂未实现，跳过
            elif self.construct_is(AC_LOAD):
                self.skip_until_next_construct()  # 暂未实现，跳过
            elif self.construct_is(PORT_DELAY):
                self.skip_until_next_construct()  # 暂未实现，跳过

            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            elif self.construct_is(USER_DATA):
                self.parse_userData()
            else:
                self.expect("|".join([DIRECTION, DESIGNATOR, PROPERTY, COMMENT, USER_DATA]))
            self.expect_end_construct()
        # 无显式 designator 时使用 port 名称作为回退
        # 与 portInstance.designator.stringDisplay 匹配使用
        port = self.elements[-1]
        if not has_designator and 'EDIF.designator' not in port.data:
            # 直接访问 _data 字典获取 EDIF.identifier
            eid = port._data.get('EDIF.identifier', '') if hasattr(port, '_data') else ''
            port['EDIF.designator'] = eid or port.name
        return self.elements.pop()

    def parse_array(self):
        self.expect(ARRAY)
        self.parse_nameDef()
        dimension_sizes = [self.parse_integerToken()]
        while self.tokenizer.is_valid_identifier():
            dimension_sizes.append(self.parse_integerToken())
        return dimension_sizes

    def parse_direction(self):
        self.expect(DIRECTION)
        direction = Port.Direction.UNDEFINED
        if self.construct_is(INOUT):
            direction = Port.Direction.INOUT
        elif self.construct_is(INPUT):
            direction = Port.Direction.IN
        elif self.construct_is(OUTPUT):
            direction = Port.Direction.OUT
        else:
            self.expect("|".join([INOUT, INPUT, OUTPUT]))
        self.tokenizer.next()
        return direction

    def parse_contents(self):
        self.expect(CONTENTS)
        while self.begin_construct():
            if self.construct_is(INSTANCE):
                instance = self.parse_instance()
                definition = self.elements[-1]
                add_exception = None
                try:
                    definition.add_child(instance)
                except ValueError as e:
                    name = instance.name
                    identifier = instance["EDIF.identifier"]
                    if name != identifier:
                        try:
                            instance.name = identifier
                            definition.add_child(instance)
                        except ValueError:
                            raise e
                    else:
                        add_exception = e
                if add_exception:
                    raise add_exception
            elif self.construct_is(NET):
                cable = self.parse_net()
                definition = self.elements[-1]
                # is_connected = False
                # for wire in cable.wires:
                #     if len(wire.pins) > 0:
                #         is_connected = True
                # if is_connected is True:
                try:
                    self.multibit_add_cable(definition, cable)
                except ValueError as e:
                    # TODO: Add warning about merging nets together
                    existing_cable = next(
                        definition.get_cables(cable.name, key="EDIF.identifier"), None
                    )
                    if existing_cable is None:
                        existing_cable = next(
                            definition.get_cables(
                                cable["EDIF.identifier"], key="EDIF.identifier"
                            )
                        )
                    for existing_wire, pending_wire in zip(existing_cable.wires, cable.wires):
                        pins = list(pending_wire.pins)
                        pending_wire.disconnect_pins_from(pins)
                        for pin in pins:
                            existing_wire.connect_pin(pin)

            elif self.construct_is(OFF_PAGE_CONNECTOR):
                self.parse_offpageconnector()
            elif self.construct_is(FIGURE):
                self.parse_figure()
            elif self.construct_is(SECTION):
                self.skip_until_next_construct()
            elif self.construct_is(NET_BUNDLE):
                self.skip_until_next_construct()
            elif self.construct_is(PAGE):
                self.parse_page()
            elif self.construct_is(COMMENT_GRAPHICS):
                self.parse_commentgraphics()
            elif self.construct_is(PORT_IMPLEMENTATION):
                self.parse_portImplementation()
            elif self.construct_is(TIMING):
                self.skip_until_next_construct()
            elif self.construct_is(SIMULATE):
                self.skip_until_next_construct()
            elif self.construct_is(WHEN):
                self.skip_until_next_construct()
            elif self.construct_is(FOLLOW):
                self.skip_until_next_construct()
            elif self.construct_is(LOGIC_PORT):
                self.skip_until_next_construct()
            elif self.construct_is(BOUNDING_BOX):
                self.skip_until_next_construct()

            elif self.construct_is(COMMENT):
                self.parse_comment()
            elif self.construct_is(USER_DATA):
                self.parse_userData()
            else:
                self.expect("|".join([INSTANCE, NET, OFF_PAGE_CONNECTOR, FIGURE, PAGE,
                    COMMENT_GRAPHICS, PORT_IMPLEMENTATION, COMMENT, USER_DATA]))
            self.expect_end_construct()

    def parse_instance(self):
        self.append_new_element(Instance())
        self.expect(INSTANCE)
        self.parse_nameDef()
        if self.begin_construct():
            if self.construct_is(VIEW_REF):
                definition = self.parse_viewRef()
                instance = self.elements[-1]
                instance.reference = definition

            elif self.construct_is(VIEW_LIST):
                self.skip_until_next_construct()  # 暂未实现，跳过
            else:
                self.expect(VIEW_REF)
            self.expect_end_construct()

        while self.begin_construct():
            if self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            elif self.construct_is(USER_DATA):
                self.parse_userData()
            elif self.construct_is(TRANSFORM):
                self.parse_transform()
            elif self.construct_is(DESIGNATOR):
                self.parse_designator()
            elif self.construct_is(ANNOTATE):
                self.parse_annotate()
            elif self.construct_is(FIGURE_GROUP_OVERRIDE):
                self.parse_figureGroupOverride()
            else:
                self.skip_until_next_construct()
            self.expect_end_construct()
        # 从 reference definition 复制 port 数据到 instance
        instance = self.elements[-1]
        if instance.reference is not None:
            ref_def = instance.reference
            if 'EDIF.portnums' in ref_def.data:
                port_count = ref_def['EDIF.portnums']
                instance['EDIF.portnums'] = port_count
                for key in ref_def.data.keys():
                    if key.startswith('EDIF.port'):
                        instance[key] = ref_def[key]
        return self.pop_element()

    def parse_viewRef(self):
        self.prefix_append("viewRef")
        self.expect(VIEW_REF)
        self.parse_nameRef()
        view_identifier = self.elements[-1].pop("EDIF.viewRef.identifier")
        definition = self.elements[-2]
        if self.begin_construct():
            definition = self.parse_cellRef()
            self.expect_end_construct()
        # 注释掉视图一致性校验以容错不规范 EDIF
        # if definition["EDIF.view.identifier"].lower() != view_identifier.lower():
        #     raise RuntimeError(
        #         "Parser error, non-existant view referenced on line {}, \
        #         revieved {} expected {}".format(
        #             self.tokenizer.line_number,
        #             view_identifier,
        #             definition["EDIF.view.identifier"],
        #         )
        #     )
        self.prefix_pop()
        return definition

    def parse_cellRef(self):
        self.prefix_append("cellRef")
        self.expect(CELL_REF)
        self.parse_nameRef()
        definition_identifer = self.elements[-1].pop("EDIF.viewRef.cellRef.identifier")
        library = self.elements[-3]
        if self.begin_construct():
            library = self.parse_libraryRef()
            self.expect_end_construct()
        definition = next(
            library.get_definitions(definition_identifer, key="EDIF.identifier"), None
        )
        assert definition is not None, (
            "Definition not found within library by EDIF identifier. definition: "
            + definition_identifer
            + " in "
            + library.name
        )
        self.prefix_pop()
        return definition

    def parse_libraryRef(self):
        self.prefix_append("libraryRef")
        self.expect(LIBRARY_REF)
        self.parse_nameRef()
        library_identifier = self.elements[-1].pop("EDIF.viewRef.cellRef.libraryRef.identifier")
        # 动态查找 Netlist 和 Library (而非用固定索引用 elements[-4]/[-3])
        environment = self.elements[0]  # Netlist 始终是栈底第一个元素
        library = None
        for elem in reversed(self.elements):
            if isinstance(elem, Library):
                library = elem
                break
        if library is None:
            library = self.elements[-3]  # 回退到固定索引
        if library["EDIF.identifier"].lower() != library_identifier.lower():
            library = next(
                environment.get_libraries(library_identifier, key="EDIF.identifier"),
                None,
            )
            assert library is not None, (
                "Library not found within netlist by EDIF identifier "
                + library_identifier
            )
        self.prefix_pop()
        return library

    def parse_net(self):
        self.append_new_element(Cable())
        self.expect(NET)
        self.parse_nameDef()
        self.elements[-1].is_scalar = True
        self.elements[-1].create_wires(1)  # EDIF nets are single wire cables.
        self.parse_construct(self.parse_joined)

        # 跟踪 figure 计数用于生成编号命名空间
        self._figure_counter = 0
        self._figures_data = []  # 收集每个 figure 的坐标数据
        while self.begin_construct():
            if self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            elif self.construct_is(USER_DATA):
                self.parse_userData()
            elif self.construct_is(FIGURE):
                self._figure_counter += 1
                self.parse_figure()
            elif self.construct_is(ARRAY):
                self.parse_array()
            elif self.construct_is(NAME):
                self.parse_name()
            elif self.construct_is(RENAME):
                self.parse_rename()
            elif self.construct_is(INSTANCE):
                # net 内的 instance 解析
                self.parse_instance()
            else:
                self.expect("|".join([PROPERTY, COMMENT, USER_DATA, FIGURE, ARRAY, NAME, RENAME, INSTANCE]))
            self.expect_end_construct()
        # 后处理: 确保编号命名空间完整
        # parse_figure 在 net 上下文中已生成 EDIF.figure{n}.figure.* 路径
        # 这里补充 figurenums 计数
        cable = self.elements[-1]
        cable["EDIF.figurenums"] = self._figure_counter
        del self._figure_counter
        del self._figures_data
        return self.pop_element()

    def parse_joined(self):
        self.expect(JOINED)
        while self.begin_construct():
            if self.construct_is(PORT_REF):
                pin = self.parse_portRef()
                if pin is not None:
                    wire = self.elements[-1].wires[0]
                    wire.connect_pin(pin)
            elif self.construct_is(PORT_LIST):
                self.skip_until_next_construct()
            elif self.construct_is(GLOBAL_PORT_REF):
                self.skip_until_next_construct()
            else:
                self.expect(PORT_REF)
            self.expect_end_construct()

    def parse_portRef(self):
        self.prefix_append("portRef")
        self.expect(PORT_REF)
        index = 0
        instance_or_definition = self.elements[-2]
        if self.begin_construct():
            indicies = self.parse_member()
            assert len(indicies) == 1
            index = indicies[0]
            self.expect_end_construct()
        else:
            self.parse_nameRef()

        while self.begin_construct():
            if self.construct_is(PORT_REF):
                self.skip_until_next_construct()
            elif self.construct_is(INSTANCE_REF):
                instance_or_definition = self.parse_instanceRef()
            elif self.construct_is(VIEW_REF):
                self.skip_until_next_construct()
            self.expect_end_construct()
        port_identifier = self.elements[-1].pop("EDIF.portRef.identifier")
        if isinstance(instance_or_definition, Instance):
            definition = instance_or_definition.reference
            port = next(
                definition.get_ports(port_identifier, key="EDIF.identifier"), None
            )
            if port is None:
                # TODO: 端口未找到时容错返回 None
                self.prefix_pop()
                return None
            inner_pin = port.pins[index]
            pin = instance_or_definition.pins[inner_pin]
        else:
            port = next(
                instance_or_definition.get_ports(
                    port_identifier, key="EDIF.identifier"
                ),
                None,
            )
            if port is None:
                # TODO: 端口未找到时容错返回 None
                self.prefix_pop()
                return None
            pin = port.pins[index]
        self.prefix_pop()
        return pin

    def parse_instanceRef(self):
        self.prefix_append("instanceRef")
        # 动态查找父 Definition (兼容 page 内的不同栈深度)
        definition = None
        for elem in reversed(self.elements):
            if isinstance(elem, Definition):
                definition = elem
                break
        if definition is None:
            definition = self.elements[-2]
        self.expect(INSTANCE_REF)
        if self.begin_construct():
            self.parse_member()
            self.skip_until_next_construct()  # 暂未实现，跳过
            self.expect_end_construct()
        else:
            self.parse_nameRef()
        instance_identifier = self.elements[-1].pop(
            "EDIF.portRef.instanceRef.identifier"
        )
        instance = next(
            definition.get_instances(instance_identifier, key="EDIF.identifier"), None
        )
        assert (
            instance is not None
        ), "Instance not found within definition by EDIF identifier"
        self.prefix_pop()
        return instance

    def parse_member(self):
        self.expect(MEMBER)
        self.parse_nameDef()
        indicies = [self.parse_integerToken()]
        while self.not_end_construct():
            indicies.append(self.parse_integerToken())
            self.expect_end_construct()
        return indicies

    def parse_viewMap(self):
        self.expect(VIEW_MAP)
        self.skip_until_next_construct()  # 暂未实现，跳过

    def parse_design(self):
        self.expect(DESIGN)
        # self.tokenizer.next()
        instance = Instance()
        instance["metadata_prefix"] = []
        self.elements.append(instance)
        instance["metadata_prefix"] = ["EDIF"]
        if self.begin_construct():
            self.parse_rename()
            self.tokenizer.next()
        else:
            self.prefix_append("identifier")
            self.set_attribute(self.parse_identifier())
            self.prefix_pop()
        self.prefix_pop()
        self.tokenizer.next()
        self.tokenizer.next()
        definition_name = self.tokenizer.next()
        self.tokenizer.next()
        self.tokenizer.next()
        library_name = self.tokenizer.next()
        for library in self.elements[0].libraries:
            if library["EDIF.identifier"] == library_name:
                break
        for definition in library.definitions:
            if definition["EDIF.identifier"] == definition_name:
                break
        instance.reference = definition
        self.elements.pop()
        self.elements[0].top_instance = instance
        self.skip_until_next_construct()

    def parse_dataOrigin(self):
        self.expect(DATA_ORIGIN)
        self.prefix_append("dataOrigin")
        name = self.parse_stringToken()
        self.set_attribute(name)
        while self.begin_construct():
            if self.construct_is(VERSION):
                self.expect(VERSION)
                self.set_attribute(self.parse_stringToken())
            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            elif self.construct_is(USER_DATA):
                self.parse_userData()
            else:
                self.expect("|".join([VERSION, PROPERTY, COMMENT, USER_DATA]))
            self.expect_end_construct()
        self.prefix_pop()

    def parse_userData(self):
        self.expect(USER_DATA)
        self.prefix_append("userData")
        # userData 可能有一个名称 (如 backgroundColor)，检查下一个 token 是否不是 ( 或 )
        peeked = self.tokenizer.peek()
        if peeked is not None and peeked != LEFT_PAREN and peeked != RIGHT_PAREN:
            if re.match(r"[a-zA-Z&]", peeked):
                self.set_attribute(self.parse_identifier())
        # userData 可能包含各种子构造 (如 color, owner 等)
        while self.begin_construct():
            if self.construct_is(COLOR):
                self.expect(COLOR)
                color = (self.parse_integerToken(), self.parse_integerToken(), self.parse_integerToken())
                self.set_attribute({"color": color})
            elif self.construct_is(OWNER):
                self.parse_owner()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            else:
                self.skip_until_next_construct()
            self.expect_end_construct()
        self.prefix_pop()

    def parse_comment(self):
        self.prefix_append("comments")
        self.expect(COMMENT)
        comment = []
        while self.not_end_construct():
            comment.append(self.parse_stringToken())
        comment = (*comment,)
        self.append_attribute(comment)
        self.prefix_pop()

    def parse_property(self):
        self.prefix_append("properties")
        self.expect(PROPERTY)
        self.parse_property_like_element()

    def parse_metax(self):
        self.prefix_append("metaxes")
        self.expect(METAX)
        self.parse_property_like_element()

    def parse_property_like_element(self):
        self.parse_nameDef()

        property_ = {}
        identifier = self.elements[-1].pop(
            ".".join([*self.elements[-1]["metadata_prefix"], "identifier"])
        )
        property_["identifier"] = identifier

        original_identifier_prefix = ".".join(
            [*self.elements[-1]["metadata_prefix"], "original_identifier"]
        )
        if original_identifier_prefix in self.elements[-1]:
            original_identifier = self.elements[-1].pop(original_identifier_prefix)
            property_["original_identifier"] = original_identifier

        value = self.parse_construct(self.parse_typedValue)
        property_["value"] = value

        self.append_attribute(property_)

        has_owner = False
        has_unit = False
        while self.begin_construct():
            if self.construct_is(OWNER):
                has_owner = self.check_for_multiples(OWNER, has_owner)
                self.parse_owner()

            elif self.construct_is(UNIT):
                has_unit = self.check_for_multiples(UNIT, has_unit)
                self.skip_until_next_construct()  # 暂时跳过 UNIT 解析

            elif self.construct_is(PROPERTY):
                self.parse_property()  # 递归解析嵌套 property
            elif self.construct_is(COMMENT):
                self.parse_comment()  # 解析嵌套 comment
            self.expect_end_construct()
        self.prefix_pop()

    def parse_typedValue(self):
        if self.construct_is(BOOLEAN):
            return self.parse_boolean()
        if self.construct_is(INTEGER):
            return self.parse_integer()
        if self.construct_is(MI_NO_MAX):
            self.expect(MI_NO_MAX)
            return self.parse_integerToken()  # 返回 minomax 数值
        if self.construct_is(NUMBER):
            return self.parse_number()
        if self.construct_is(POINT):
            self.expect(POINT)
            # point 可以是直接的两个整数，也可以是 (pt x y) 子构造
            if self.tokenizer.peek_equals(LEFT_PAREN):
                result = None
                while self.begin_construct():
                    if self.construct_is(PT):
                        self.expect(PT)
                        result = (self.parse_integerToken(), self.parse_integerToken())
                    else:
                        self.expect(PT)
                    self.expect_end_construct()
                return result
            else:
                return (self.parse_integerToken(), self.parse_integerToken())
        if self.construct_is(STRING):
            return self.parse_string()
        return self.expect("|".join([BOOLEAN, INTEGER, NUMBER, STRING]))

    def parse_boolean(self):
        self.expect(BOOLEAN)
        # 支持两种模式:
        # 1. 简单: (boolean true) / (boolean false)
        # 2. 嵌套: (boolean (boolean (true)) (boolean (true)) ...)
        results = []
        while self.begin_construct():
            if self.construct_is(BOOLEAN):
                results.append(self.parse_boolean())
            elif self.construct_is(TRUE):
                self.tokenizer.next()
                results.append(True)
            elif self.construct_is(FALSE):
                self.tokenizer.next()
                results.append(False)
            elif self.tokenizer.peek_equals(TRUE):
                self.tokenizer.next()
                results.append(True)
            elif self.tokenizer.peek_equals(FALSE):
                self.tokenizer.next()
                results.append(False)
            else:
                pass  # 不在预期中的构造则跳过
            self.expect_end_construct()
        if len(results) == 0:
            # 无子构造，可能是直接 token 形式: (boolean false)
            if self.tokenizer.peek_equals(TRUE):
                self.tokenizer.next()
                return True
            elif self.tokenizer.peek_equals(FALSE):
                self.tokenizer.next()
                return False
            else:
                return True  # 默认
        return results[0] if len(results) == 1 else results

    def parse_integer(self):
        self.expect(INTEGER)
        return self.parse_integerToken()

    def parse_number(self):
        self.expect(NUMBER)
        if self.begin_construct():
            result = self.parse_construct(self.parse_e)
            self.expect_end_construct()
        else:
            result = self.parse_integerToken()
        return result

    def parse_e(self):
        self.expect(E)
        mantissa = self.parse_integerToken()
        exponent = self.parse_integerToken()
        result = mantissa * 10.0**exponent
        return result

    def parse_string(self):
        self.expect(STRING)
        # 如果下一个 token 是左括号，则存在子构造 (stringDisplay/stringValue)
        if self.tokenizer.peek_equals(LEFT_PAREN):
            result = None
            while self.begin_construct():
                if self.construct_is(STRING_DISPLAY):
                    result = self.parse_stringDisplay()
                elif self.construct_is(STRING_VALUE):
                    result = self.parse_stringValue()
                elif self.construct_is(PROPERTY):
                    self.parse_property()
                elif self.construct_is(COMMENT):
                    self.parse_comment()
                else:
                    self.expect("|".join([STRING_DISPLAY, STRING_VALUE]))
                self.expect_end_construct()
            return result
        else:
            return self.parse_stringToken()

    def parse_stringDisplay(self):
        self.expect(STRING_DISPLAY)
        self.prefix_append("stringDisplay")
        text = self.parse_stringToken()
        self.set_attribute(text)
        while self.begin_construct():
            if self.construct_is(DISPLAY):
                self.parse_display()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            else:
                self.expect("|".join([DISPLAY, PROPERTY, COMMENT]))
            self.expect_end_construct()
        self.prefix_pop()
        return text

    def parse_stringValue(self):
        self.expect(STRING_VALUE)
        self.prefix_append("stringValue")
        val = self.parse_stringToken()
        self.set_attribute(val)
        self.prefix_pop()
        return val

    def parse_display(self):
        self.expect(DISPLAY)
        self.prefix_append("display")
        # display 通常有一个名称 (如 DISPLAYPROPERTY)，检查 peek token
        peeked = self.tokenizer.peek()
        if peeked is not None and peeked != LEFT_PAREN and peeked != RIGHT_PAREN:
            if re.match(r"[a-zA-Z&]", peeked):
                self.set_attribute(self.parse_identifier())
        while self.begin_construct():
            if self.construct_is(JUSTIFY):
                self.parse_justify()
            elif self.construct_is(ORIGIN):
                self.parse_origin()
            elif self.construct_is(ORIENTATION):
                self.parse_orientation()
            elif self.construct_is(VISIBLE):
                self.expect(VISIBLE)
                # visible 可能是布尔构造或字符串
                if self.tokenizer.peek_equals(LEFT_PAREN):
                    self.skip_until_next_construct()
                else:
                    self.set_attribute({"visible": self.parse_stringToken()})
            elif self.construct_is(FIGURE_GROUP_OVERRIDE):
                self.parse_figureGroupOverride()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            else:
                self.expect("|".join([JUSTIFY, ORIGIN, ORIENTATION, VISIBLE,
                    FIGURE_GROUP_OVERRIDE, PROPERTY, COMMENT]))
            self.expect_end_construct()
        self.prefix_pop()

    def parse_justify(self):
        self.expect(JUSTIFY)
        self.prefix_append("justify")
        self.set_attribute(self.parse_identifier())
        self.prefix_pop()

    def parse_origin(self):
        self.expect(ORIGIN)
        self.prefix_append("origin")
        while self.begin_construct():
            if self.construct_is(PT):
                self.parse_pt()
            else:
                self.expect(PT)
            self.expect_end_construct()
        self.prefix_pop()

    def parse_orientation(self):
        self.expect(ORIENTATION)
        self.prefix_append("orientation")
        self.set_attribute(self.parse_identifier())
        self.prefix_pop()

    def parse_pt(self):
        """解析 pt 点坐标"""
        self.expect(PT)
        self.prefix_append("pt")
        x = self.parse_integerToken()
        y = self.parse_integerToken()
        self.set_attribute((x, y))
        self.prefix_pop()

    def parse_owner(self):
        self.expect(OWNER)
        self.parse_stringToken()

    def parse_unit(self):
        self.skip_until_next_construct()  # 暂未实现，跳过

    def parse_nameRef(self):
        self.prefix_append("identifier")
        self.set_attribute(self.parse_identifier())
        self.prefix_pop()

    def parse_nameDef(self):
        if self.begin_construct():
            # 支持 (rename ...) 和 (name ...) 两种形式
            if self.construct_is(RENAME):
                self.parse_rename()
            elif self.construct_is(NAME):
                self.expect(NAME)
                self.prefix_append("identifier")
                self.set_attribute(self.parse_identifier())
                self.prefix_pop()
                while self.begin_construct():
                    self.skip_until_next_construct()
                    self.expect_end_construct()
            else:
                self.expect("|".join([RENAME, NAME]))
            self.expect_end_construct()
        else:
            self.prefix_append("identifier")
            self.set_attribute(self.parse_identifier())
            self.prefix_pop()

    def multibit_add_cable(self, definition, cable):
        c_edif_id = cable["EDIF.identifier"]
        c_name = cable.name

        e_index, e_short = self.separate_name_and_index(c_edif_id, "_")
        n_index, n_short = self.separate_name_and_index(c_name, "[")

        index = n_index
        if e_index is None:
            index = None

        existing_cable = next(definition.get_cables(n_short), None)
        if existing_cable is None:  # maybe the name is in the EDIF.identifier only?
            existing_cable = next(
                definition.get_cables(e_short, key="EDIF.identifier"), None
            )
        if existing_cable is None:  # if it is still none after checking both the name and EDIF.identifier...
            if index is None:
                cable.is_array = False
                cable.lower_index = 0
            else:
                cable.is_array = True
                if "EDIF.identifier" in cable:
                    cable["EDIF.identifier"] = e_short
                cable.name = n_short
                cable.lower_index = index
            definition.add_cable(cable)

        else:  # there is alread a cable that could need to be merged.
            if existing_cable.is_array is False or index is None:
                definition.add_cable(cable)  # if this works great. otherwise the parent code will handle the error
            else:  # the cables should be merged
                if index > existing_cable.lower_index:
                    if index < existing_cable.lower_index + len(existing_cable.wires):
                        w = cable.wires[0]
                        ew = existing_cable.wires[index - existing_cable.lower_index]
                        pins = w.pins
                        while len(pins) > 0:
                            p = pins[0]
                            w.disconnect_pin(p)
                            ew.connect_pin(p)
                    else:  # index is outside current cable range
                        existing_cable.create_wires(
                            index - existing_cable.lower_index - len(existing_cable.wires)
                        )
                        wire = cable.wires[0]
                        cable.remove_wire(wire)
                        existing_cable.add_wire(wire)
                else:  # index is lower than the lowest current index in the cable
                    difference = existing_cable.lower_index - index
                    starting_count = len(existing_cable.wires)
                    wire = cable.wires[0]
                    cable.remove_wire(wire)
                    existing_cable.add_wire(wire)
                    existing_cable.create_wires(difference - 1)
                    existing_cable.lower_index = index
                    wire_list = (
                        existing_cable.wires[starting_count:]
                        + existing_cable.wires[:starting_count]
                    )
                    existing_cable.wires = wire_list

    def separate_name_and_index(self, name, split_character):
        name_split = name.split(split_character)
        index = None
        short_name = name
        if split_character == "[" and (
            name[0] != "\\" or (len(name.split(" ")) == 2 and name.split(" ")[1] != "")
        ):
            if (
                len(name_split) > 1
                and name_split[-1][-1] == "]"
                and name_split[-1][:-1].isdigit()
            ):
                index = int(name_split[-1][:-1])
                for i in reversed(range(len(name))):
                    if name[i] == split_character:
                        break
                short_name = name[:i]
        elif split_character == "_":  # and (name[0:2] == "&_" or ():
            # Assuming that all names that start with a &_ map to escaped \
            #
            # from https://www.xilinx.com/support/answers/1554.html
            #
            # "When the Cadence SIR2EDF encounters escaped Verilog names (please
            # refer to (Xilinx Answer 2533)), "\L/R " is mapped by the Cadence
            # SIR2EDF netlister to "&_l_r_". The SIR2EDF netlister also creates
            # a map file, which shows that the identifier "&_l_r_" is mapped to
            # "\l/r ". Such conversions of backslashes and forward slashes may
            # be fairly common in netlists generated by NGD2VER if
            # user-specified names do not conform to Verilog naming
            # restrictions."
            #
            # Other than here we try to maintain the user supplied name and do
            # not change characters. a name starting with &_ will simply become

            if (
                len(name_split) > 2
                and name_split[-1] == ""
                and name_split[-2].isdigit()
                and (name[0:2] != "&_" or (name_split[-3] == ""))
            ):
                index = int(name_split[-2])
                count = 0
                for i in reversed(range(len(name))):
                    if name[i] == split_character:
                        count += 1
                    if count == 2:
                        break
                short_name = name[:i]
        return index, short_name

    def parse_rename(self):
        self.expect(RENAME)

        self.prefix_append("identifier")
        # rename 可能包含 (name ...) 子构造或直接是标识符
        if self.begin_construct():
            if self.construct_is(NAME):
                self.expect(NAME)
                self.set_attribute(self.parse_identifier())
                # 跳过 (name ...) 中的 display 等子构造
                while self.begin_construct():
                    self.skip_until_next_construct()
                    self.expect_end_construct()
            else:
                self.set_attribute(self.parse_identifier())
            self.expect_end_construct()
        else:
            self.set_attribute(self.parse_identifier())
        self.prefix_pop()

        self.prefix_append("original_identifier")
        self.set_attribute(self.parse_stringToken())
        self.prefix_pop()

    def parse_identifier(self):
        self.tokenizer.next()
        self.tokenizer.expect_valid_identifier()
        return self.tokenizer.token

    def parse_stringToken(self):
        self.tokenizer.next()
        self.tokenizer.expect_valid_stringToken()
        return self.tokenizer.token[1:-1]

    def parse_integerToken(self):
        self.tokenizer.next()
        self.tokenizer.expect_valid_integerToken()
        return int(self.tokenizer.token)

    def append_new_element(self, element):
        element["metadata_prefix"] = ["EDIF"]
        self.elements.append(element)

    def pop_element(self):
        element = self.elements.pop()
        del element["metadata_prefix"]
        return element

    def prefix_append(self, value):
        element = self.elements[-1]
        element["metadata_prefix"].append(value)

    def prefix_pop(self):
        return self.elements[-1]["metadata_prefix"].pop()

    def set_attribute(self, value):
        element = self.elements[-1]
        key = ".".join(element["metadata_prefix"])
        if key == "EDIF.original_identifier":
            element.name = value
        elif key == "EDIF.identifier":
            if element.name is None:
                element.name = value
            element[key] = value
        else:
            element[key] = value

    def append_attribute(self, attribute):
        element = self.elements[-1]
        key = ".".join(element["metadata_prefix"])
        if key not in element:
            element[key] = []
        element[key].append(attribute)

    # ============================================================
    # 图形 / 符号 / 页面 解析方法 (DIR1 扩展)
    # ============================================================

    def parse_symbol(self):
        """解析 symbol 构造"""
        self.expect(SYMBOL)
        self.prefix_append("symbol")
        # 收集多个 portImplementation 的数据
        self._port_data_list = []
        while self.begin_construct():
            if self.construct_is(FIGURE):
                self.parse_figure()
            elif self.construct_is(FIGURE_GROUP):
                self.parse_figureGroup()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            elif self.construct_is(USER_DATA):
                self.parse_userData()
            elif self.construct_is(INSTANCE):
                instance = self.parse_instance()
                definition = self.elements[-1]
                try:
                    definition.add_child(instance)
                except ValueError:
                    if "EDIF.identifier" in instance:
                        instance.name = instance["EDIF.identifier"]
                        try:
                            definition.add_child(instance)
                        except ValueError:
                            pass
            elif self.construct_is(PORT_IMPLEMENTATION):
                # 收集当前 port 的命名空间快照
                port_data = {}
                self._port_data_list.append(port_data)
                self.parse_portImplementation()
                # 解析完成后从元素读取该 port 写入的键
                base = "EDIF.view.symbol.portImplementation"
                element = self.elements[-1]
                for key in list(element.data.keys()):
                    if key.startswith(base + "."):
                        sub_key = key[len(base):]
                        port_data[sub_key] = element[key]
                # 清除已收集的键以避免后续 port 混淆
                for key in list(element.data.keys()):
                    if key.startswith(base + "."):
                        del element[key]
            elif self.construct_is(ANNOTATE):
                self.parse_annotate()
            elif self.construct_is(COMMENT_GRAPHICS):
                self.parse_commentgraphics()
            elif self.construct_is(CONNECT_LOCATION):
                self.parse_connectLocation()
            elif self.construct_is(KEYWORD_DISPLAY):
                self.parse_keywordDisplay()
            elif self.construct_is(NAME):
                self.parse_name()
            elif self.construct_is(PROPERTY_DISPLAY):
                self.parse_propertyDisplay()
            else:
                self.expect("|".join([FIGURE, FIGURE_GROUP, INSTANCE, PORT_IMPLEMENTATION,
                    ANNOTATE, COMMENT_GRAPHICS, CONNECT_LOCATION,
                    KEYWORD_DISPLAY, NAME, PROPERTY_DISPLAY,
                    PROPERTY, COMMENT, USER_DATA]))
            self.expect_end_construct()
        # 后处理: 生成多端口编号命名空间
        self._build_port_numbers()
        del self._port_data_list
        self.prefix_pop()

    def _build_port_numbers(self):
        """后处理 symbol: 从收集的 port 数据生成 EDIF.port{n}.* 编号命名空间

        关键修复: designator.stringDisplay 值对齐到对应 Port 的 EDIF.designator,
        确保 model_get.py / parse_wcca.py 的 port-to-portInstance 匹配能正常工作.

        映射规则:
        - .name -> .name (保持不变)
        - .keywordDisplay -> .designator (关键字重命名)
        - .keywordDisplay.display.X -> .designator.stringDisplay.X (子键)
        - .keywordDisplay.display -> .designator.stringDisplay = port 的 designator (对齐匹配键)
        """
        element = self.elements[-1]
        port_count = len(self._port_data_list)
        if port_count == 0:
            return
        element["EDIF.portnums"] = port_count
        # 按顺序获取 Definition 的 port designator 列表
        from spydrnet.ir import Definition
        port_designators = []
        if isinstance(element, Definition):
            for p in element.ports:
                port_designators.append(p._data.get('EDIF.designator', ''))
        for i, port_data in enumerate(self._port_data_list, 1):
            pfx = f"EDIF.port{i}.portInstance"
            # 获取对应 port 的 designator (按顺序)
            port_des = port_designators[i-1] if i <= len(port_designators) else str(i)
            has_designator_display = False  # 是否已设置 designator.stringDisplay
            for raw_key, value in port_data.items():
                if ".keywordDisplay" in raw_key:
                    mapped = raw_key.replace(".keywordDisplay", ".designator", 1)
                    if mapped.startswith(".designator.display"):
                        if mapped == ".designator.display":
                            # 叶值: .keywordDisplay.display -> .designator.stringDisplay
                            element[f"{pfx}.designator.stringDisplay"] = port_des or str(i)
                            has_designator_display = True
                            continue
                        else:
                            # 子键: .keywordDisplay.display.X -> .designator.stringDisplay.display.X
                            mapped = mapped.replace(".designator.display",
                                                    ".designator.stringDisplay.display", 1)
                    element[f"{pfx}{mapped}"] = value
                else:
                    element[f"{pfx}{raw_key}"] = value
            # 如果 port_data 没有 keywordDisplay.display, 仍然设置 designator.stringDisplay
            if not has_designator_display:
                element[f"{pfx}.designator.stringDisplay"] = port_des or str(i)

    def parse_figure(self):
        """解析 figure 构造"""
        self.expect(FIGURE)
        # 检测是否在 net 上下文中 (Cable 元素在栈顶且有 _figure_counter)
        in_net = (hasattr(self, '_figure_counter') and
                  len(self.elements) > 0 and
                  isinstance(self.elements[-1], Cable))
        fig_num = getattr(self, '_figure_counter', 0)
        if in_net:
            self.prefix_append(f"figure{fig_num}")
            self.prefix_append("figure")  # 额外的 figure 段以匹配 parse_wcca 路径
        else:
            self.prefix_append("figure")
        # figure 可能有名称 (如 WIRE, PIN 等)
        figure_name = None
        peeked = self.tokenizer.peek()
        if peeked is not None and peeked != LEFT_PAREN and peeked != RIGHT_PAREN:
            if re.match(r"[a-zA-Z&]", peeked):
                figure_name = self.parse_identifier()
                self.set_attribute(figure_name)
        # 跟踪当前 figure 的点坐标 (用于 net 上下文)
        if in_net:
            self._current_figure_points = []
            self._current_figure_name = figure_name
        while self.begin_construct():
            if self.construct_is(FIGURE_GROUP_OVERRIDE):
                self.parse_figureGroupOverride()
            elif self.construct_is(DOT):
                self.parse_dot()
            elif self.construct_is(CIRCLE):
                self.parse_circle()
            elif self.construct_is(RECTANGLE):
                self.parse_rectangle()
            elif self.construct_is(POLYGON):
                self.parse_polygon()
            elif self.construct_is(PATH):
                self.parse_path()
            elif self.construct_is(OPEN_SHAPE):
                self.parse_openShape()
            elif self.construct_is(CURVE):
                self.parse_curve()
            elif self.construct_is(ARC):
                self.parse_arc()
            elif self.construct_is(ANNOTATE):
                self.parse_annotate()
            elif self.construct_is(PORT_IMPLEMENTATION):
                self.parse_portImplementation()
            elif self.construct_is(PROPERTY_DISPLAY):
                self.parse_propertyDisplay()
            elif self.construct_is(KEYWORD_DISPLAY):
                self.parse_keywordDisplay()
            elif self.construct_is(NAME):
                self.parse_name()
            elif self.construct_is(COMMENT_GRAPHICS):
                self.parse_commentgraphics()
            elif self.construct_is(CONNECT_LOCATION):
                self.parse_connectLocation()
            elif self.construct_is(TRANSFORM):
                self.parse_transform()
            elif self.construct_is(POINT_LIST):
                self.parse_pointList()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            elif self.construct_is(SHAPE):
                self.skip_until_next_construct()
            elif self.construct_is(USER_DATA):
                self.parse_userData()
            else:
                self.expect("|".join([FIGURE_GROUP_OVERRIDE, DOT, CIRCLE, RECTANGLE, POLYGON, SHAPE,
                    PATH, OPEN_SHAPE, CURVE, ARC, ANNOTATE, PORT_IMPLEMENTATION,
                    PROPERTY_DISPLAY, KEYWORD_DISPLAY, NAME, COMMENT_GRAPHICS,
                    CONNECT_LOCATION, TRANSFORM, POINT_LIST, PROPERTY, COMMENT, USER_DATA]))
            self.expect_end_construct()
        # 如果在 net 上下文中，收集 figure 数据用于后处理
        if in_net:
            self._figures_data.append({
                "figure_name": self._current_figure_name,
                "point_count": len(self._current_figure_points),
                "points": self._current_figure_points,
            })
            del self._current_figure_points
            del self._current_figure_name
        self.prefix_pop()
        if in_net:
            self.prefix_pop()  # 弹出额外的 "figure" 段

    def parse_figureGroupOverride(self):
        """解析 figureGroupOverride 构造"""
        self.expect(FIGURE_GROUP_OVERRIDE)
        self.prefix_append("figureGroupOverride")
        self.set_attribute(self.parse_identifier())
        while self.begin_construct():
            if self.construct_is(COLOR):
                self.expect(COLOR)
                color = (self.parse_integerToken(), self.parse_integerToken(), self.parse_integerToken())
                self.set_attribute({"color": color})
            elif self.construct_is(FILL_PATTERN):
                self.parse_fillPattern()
            elif self.construct_is(PATH_WIDTH):
                self.parse_pathWidth()
            elif self.construct_is(TEXT_HEIGHT):
                self.expect(TEXT_HEIGHT)
                self.set_attribute({"textHeight": self.parse_integerToken()})
            elif self.construct_is(CORNER_TYPE):
                self.expect(CORNER_TYPE)
                self.set_attribute({"cornerType": self.parse_identifier()})
            elif self.construct_is(END_TYPE):
                self.expect(END_TYPE)
                self.set_attribute({"endType": self.parse_identifier()})
            elif self.construct_is(VISIBLE):
                self.expect(VISIBLE)
                if self.tokenizer.peek_equals(LEFT_PAREN):
                    self.skip_until_next_construct()
                else:
                    self.set_attribute({"visible": self.parse_stringToken()})
            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            else:
                self.expect("|".join([COLOR, FILL_PATTERN, PATH_WIDTH, TEXT_HEIGHT,
                    CORNER_TYPE, END_TYPE, VISIBLE, PROPERTY, COMMENT]))
            self.expect_end_construct()
        self.prefix_pop()

    def parse_connectLocation(self):
        """解析 connectLocation 构造"""
        self.expect(CONNECT_LOCATION)
        self.prefix_append("connectLocation")
        while self.begin_construct():
            if self.construct_is(FIGURE):
                self.parse_figure()
            else:
                self.expect(FIGURE)
            self.expect_end_construct()
        self.prefix_pop()

    def parse_dot(self):
        """解析 dot 点形状"""
        self.expect(DOT)
        self.prefix_append("dot")
        while self.begin_construct():
            if self.construct_is(PT):
                self.parse_pt()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            else:
                self.expect("|".join([PT, PROPERTY]))
            self.expect_end_construct()
        self.prefix_pop()

    def parse_circle(self):
        """解析 circle 圆形状"""
        self.expect(CIRCLE)
        self.prefix_append("circle")
        while self.begin_construct():
            if self.construct_is(PT):
                self.parse_pt()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            else:
                self.expect("|".join([PT, PROPERTY]))
            self.expect_end_construct()
        self.prefix_pop()

    def parse_rectangle(self):
        """解析 rectangle 矩形形状"""
        self.expect(RECTANGLE)
        self.prefix_append("rectangle")
        while self.begin_construct():
            if self.construct_is(PT):
                self.parse_pt()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            else:
                self.expect("|".join([PT, PROPERTY]))
            self.expect_end_construct()
        self.prefix_pop()

    def parse_polygon(self):
        """解析 polygon 多边形形状"""
        self.expect(POLYGON)
        self.prefix_append("polygon")
        while self.begin_construct():
            if self.construct_is(PT):
                self.parse_pt()
            elif self.construct_is(POINT_LIST):
                self.parse_pointList()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            else:
                self.expect("|".join([PT, POINT_LIST, PROPERTY]))
            self.expect_end_construct()
        self.prefix_pop()

    def parse_openShape(self):
        """解析 openShape 开放形状"""
        self.expect(OPEN_SHAPE)
        self.prefix_append("openShape")
        while self.begin_construct():
            if self.construct_is(PT):
                self.parse_pt()
            elif self.construct_is(POINT_LIST):
                self.parse_pointList()
            elif self.construct_is(CURVE):
                self.parse_curve()
            elif self.construct_is(ARC):
                self.parse_arc()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            else:
                self.expect("|".join([PT, POINT_LIST, CURVE, ARC, PROPERTY]))
            self.expect_end_construct()
        self.prefix_pop()

    def parse_path(self):
        """解析 path 路径"""
        self.expect(PATH)
        self.prefix_append("path")
        while self.begin_construct():
            if self.construct_is(POINT_LIST):
                self.parse_pointList()
            elif self.construct_is(PT):
                self.parse_pt()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            else:
                self.expect("|".join([POINT_LIST, PT, PROPERTY]))
            self.expect_end_construct()
        self.prefix_pop()

    def parse_pointList(self):
        """解析 pointList 点列表"""
        self.expect(POINT_LIST)
        self.prefix_append("pointlist")  # 小写以匹配 parse_wcca.py 路径
        in_net = (hasattr(self, '_figure_counter') and
                  len(self.elements) > 0 and
                  isinstance(self.elements[-1], Cable))
        points = []
        point_num = 0
        while self.begin_construct():
            if self.construct_is(PT):
                point_num += 1
                # 在 net 上下文中使用编号前缀 (point1, point2, ...)
                if in_net:
                    self.prefix_append(f"point{point_num}")
                    self.prefix_append("pt")  # 添加 .pt 后缀
                self.expect(PT)
                x = self.parse_integerToken()
                y = self.parse_integerToken()
                points.append((x, y))
                if in_net:
                    self.set_attribute((x, y))
                    self.prefix_pop()  # pop "pt"
                    self.prefix_pop()  # pop f"point{point_num}"
            elif self.construct_is(PROPERTY):
                self.parse_property()
            else:
                self.expect("|".join([PT, PROPERTY]))
            self.expect_end_construct()
        if in_net:
            # 在 net 上下文中存储点到当前 figure 并添加 pointnums
            if hasattr(self, '_current_figure_points'):
                self._current_figure_points = points
            self.prefix_append("pointnums")
            self.set_attribute(point_num)
            self.prefix_pop()
        else:
            self.set_attribute(points)
        self.prefix_pop()

    def parse_curve(self):
        """解析 curve 曲线"""
        self.expect(CURVE)
        self.prefix_append("curve")
        while self.begin_construct():
            if self.construct_is(PT):
                self.parse_pt()
            elif self.construct_is(POINT_LIST):
                self.parse_pointList()
            elif self.construct_is(ARC):
                self.parse_arc()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            else:
                self.expect("|".join([PT, POINT_LIST, ARC, PROPERTY]))
            self.expect_end_construct()
        self.prefix_pop()

    def parse_arc(self):
        """解析 arc 弧线"""
        self.expect(ARC)
        self.prefix_append("arc")
        while self.begin_construct():
            if self.construct_is(PT):
                self.parse_pt()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            else:
                self.expect("|".join([PT, PROPERTY]))
            self.expect_end_construct()
        self.prefix_pop()

    def parse_transform(self):
        """解析 transform 变换"""
        self.expect(TRANSFORM)
        self.prefix_append("transform")
        while self.begin_construct():
            if self.construct_is(PT):
                self.parse_pt()
            elif self.construct_is(ORIENTATION):
                self.parse_orientation()
            elif self.construct_is(ORIGIN):
                self.parse_origin()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            else:
                self.expect("|".join([PT, ORIENTATION, ORIGIN, PROPERTY]))
            self.expect_end_construct()
        self.prefix_pop()

    def parse_annotate(self):
        """解析 annotate 注释"""
        self.expect(ANNOTATE)
        self.prefix_append("annotate")
        while self.begin_construct():
            if self.construct_is(FIGURE_GROUP_OVERRIDE):
                self.parse_figureGroupOverride()
            elif self.construct_is(DISPLAY):
                self.parse_display()
            elif self.construct_is(JUSTIFY):
                self.parse_justify()
            elif self.construct_is(ORIGIN):
                self.parse_origin()
            elif self.construct_is(ORIENTATION):
                self.parse_orientation()
            elif self.construct_is(COLOR):
                self.expect(COLOR)
                color = (self.parse_integerToken(), self.parse_integerToken(), self.parse_integerToken())
                self.set_attribute({"color": color})
            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            else:
                self.skip_until_next_construct()
            self.expect_end_construct()
        self.prefix_pop()

    def parse_portImplementation(self):
        """解析 portImplementation 构造"""
        self.expect(PORT_IMPLEMENTATION)
        self.prefix_append("portImplementation")
        # portImplementation 可能有一个类型名 (如 WIRE, BUNDLE)
        peeked = self.tokenizer.peek()
        if peeked is not None and peeked != LEFT_PAREN and peeked != RIGHT_PAREN and re.match(r"[a-zA-Z&]", peeked):
            self.set_attribute(self.parse_identifier())
        while self.begin_construct():
            if self.construct_is(CONNECT_LOCATION):
                self.parse_connectLocation()
            elif self.construct_is(NAME):
                self.parse_name()
            elif self.construct_is(KEYWORD_DISPLAY):
                self.parse_keywordDisplay()
            elif self.construct_is(PROPERTY_DISPLAY):
                self.parse_propertyDisplay()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            else:
                self.skip_until_next_construct()
            self.expect_end_construct()
        self.prefix_pop()

    def parse_propertyDisplay(self):
        """解析 propertyDisplay 属性显示"""
        self.expect(PROPERTY_DISPLAY)
        self.prefix_append("propertyDisplay")
        peeked = self.tokenizer.peek()
        if peeked is not None and peeked != LEFT_PAREN and peeked != RIGHT_PAREN and re.match(r"[a-zA-Z&]", peeked):
            self.set_attribute(self.parse_identifier())
        while self.begin_construct():
            if self.construct_is(DISPLAY):
                self.parse_display()
            elif self.construct_is(JUSTIFY):
                self.parse_justify()
            elif self.construct_is(ORIGIN):
                self.parse_origin()
            elif self.construct_is(ORIENTATION):
                self.parse_orientation()
            elif self.construct_is(FIGURE_GROUP_OVERRIDE):
                self.parse_figureGroupOverride()
            elif self.construct_is(VISIBLE):
                self.expect(VISIBLE)
                self.set_attribute({"visible": self.parse_boolean()})
            elif self.construct_is(PROPERTY):
                self.parse_property()
            else:
                self.skip_until_next_construct()
            self.expect_end_construct()
        self.prefix_pop()

    def parse_keywordDisplay(self):
        """解析 keywordDisplay 关键字显示"""
        self.expect(KEYWORD_DISPLAY)
        self.prefix_append("keywordDisplay")
        peeked = self.tokenizer.peek()
        if peeked is not None and peeked != LEFT_PAREN and peeked != RIGHT_PAREN and re.match(r"[a-zA-Z&]", peeked):
            self.set_attribute(self.parse_identifier())
        while self.begin_construct():
            if self.construct_is(DISPLAY):
                self.parse_display()
            elif self.construct_is(JUSTIFY):
                self.parse_justify()
            elif self.construct_is(ORIGIN):
                self.parse_origin()
            elif self.construct_is(ORIENTATION):
                self.parse_orientation()
            elif self.construct_is(FIGURE_GROUP_OVERRIDE):
                self.parse_figureGroupOverride()
            elif self.construct_is(VISIBLE):
                self.expect(VISIBLE)
                self.set_attribute({"visible": self.parse_boolean()})
            elif self.construct_is(PROPERTY):
                self.parse_property()
            else:
                self.skip_until_next_construct()
            self.expect_end_construct()
        self.prefix_pop()

    def parse_name(self):
        """解析 name 名称显示 (图形中)"""
        self.expect(NAME)
        self.prefix_append("name")
        peeked = self.tokenizer.peek()
        if peeked is not None and peeked != LEFT_PAREN and peeked != RIGHT_PAREN and re.match(r"[a-zA-Z&]", peeked):
            self.set_attribute(self.parse_identifier())
        while self.begin_construct():
            if self.construct_is(DISPLAY):
                self.parse_display()
            elif self.construct_is(JUSTIFY):
                self.parse_justify()
            elif self.construct_is(ORIGIN):
                self.parse_origin()
            elif self.construct_is(ORIENTATION):
                self.parse_orientation()
            elif self.construct_is(FIGURE_GROUP_OVERRIDE):
                self.parse_figureGroupOverride()
            elif self.construct_is(VISIBLE):
                self.expect(VISIBLE)
                self.set_attribute({"visible": self.parse_boolean()})
            elif self.construct_is(PROPERTY):
                self.parse_property()
            else:
                self.skip_until_next_construct()
            self.expect_end_construct()
        self.prefix_pop()

    def parse_commentgraphics(self):
        """解析 commentGraphics 图形注释"""
        self.expect(COMMENT_GRAPHICS)
        self.prefix_append("commentGraphics")
        while self.begin_construct():
            if self.construct_is(FIGURE):
                self.parse_figure()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            elif self.construct_is(ANNOTATE):
                self.parse_annotate()
            elif self.construct_is(STRING_DISPLAY):
                self.parse_stringDisplay()
            else:
                self.skip_until_next_construct()
            self.expect_end_construct()
        self.prefix_pop()

    def parse_offpageconnector(self):
        """解析 offPageConnector 跨页连接器"""
        self.expect(OFF_PAGE_CONNECTOR)
        self.prefix_append("offPageConnector")
        if self.begin_construct():
            self.parse_rename()
            self.expect_end_construct()
        else:
            self.set_attribute(self.parse_identifier())
        self.prefix_pop()

    def parse_page(self):
        """解析 page 页面 - 将 instance/net 挂载到父 Definition"""
        self.expect(PAGE)
        self.prefix_append("page")
        # page 可能有名称标识符
        peeked = self.tokenizer.peek()
        if peeked is not None and peeked != LEFT_PAREN and peeked != RIGHT_PAREN:
            self.set_attribute(self.parse_identifier())
        # 在栈中查找父 Definition (用于挂载 instance/cable)
        parent_def = None
        for elem in reversed(self.elements):
            if isinstance(elem, Definition):
                parent_def = elem
                break
        while self.begin_construct():
            if self.construct_is(INSTANCE):
                instance = self.parse_instance()
                if parent_def is not None:
                    try:
                        parent_def.add_child(instance)
                    except ValueError:
                        if "EDIF.identifier" in instance:
                            instance.name = instance["EDIF.identifier"]
                            try:
                                parent_def.add_child(instance)
                            except ValueError:
                                pass
            elif self.construct_is(NET):
                cable = self.parse_net()
                if parent_def is not None:
                    try:
                        self.multibit_add_cable(parent_def, cable)
                    except ValueError:
                        pass
            elif self.construct_is(FIGURE):
                self.parse_figure()
            elif self.construct_is(COMMENT_GRAPHICS):
                self.parse_commentgraphics()
            elif self.construct_is(OFF_PAGE_CONNECTOR):
                self.parse_offpageconnector()
            elif self.construct_is(PAGE):
                self.parse_page()
            elif self.construct_is(PROPERTY):
                self.parse_property()
            elif self.construct_is(COMMENT):
                self.parse_comment()
            elif self.construct_is(USER_DATA):
                self.parse_userData()
            else:
                self.skip_until_next_construct()
            self.expect_end_construct()
        self.prefix_pop()

    # ============================================================
    # 辅助方法
    # ============================================================

    def skip_until_next_construct(self):
        count = 0
        while count > 0 or not self.tokenizer.peek_equals(RIGHT_PAREN):
            if self.tokenizer.peek_equals(LEFT_PAREN):
                count += 1
            elif self.tokenizer.peek_equals(RIGHT_PAREN):
                count -= 1
            self.tokenizer.next()

    def check_for_multiples(self, token, already_contains):
        if already_contains:
            raise RuntimeError(
                "Parse error: Multiple occurances of {}, near line {}".format(
                    token, self.tokenizer.line_number
                )
            )
        return True

    def expect_begin_construct(self):
        self.tokenizer.next()
        if LEFT_PAREN != self.tokenizer.token:
            self.tokenizer.expect(LEFT_PAREN)

    def not_end_construct(self):
        if RIGHT_PAREN != self.tokenizer.peek():
            return True
        return False

    def expect_end_construct(self):
        self.tokenizer.next()
        if RIGHT_PAREN != self.tokenizer.token:
            self.tokenizer.expect(RIGHT_PAREN)

    def expect(self, token):
        self.tokenizer.next()
        self.tokenizer.expect(token)

    def begin_construct(self):
        if LEFT_PAREN == self.tokenizer.peek():
            self.tokenizer.next()
            return True
        return False

    def construct_is(self, token):
        if self.tokenizer.peek_equals(token):
            return True
        return False

    get_parent = {
        Library: lambda x: x.netlist,
        Definition: lambda x: x.library,
        Port: lambda x: x.definition,
        Cable: lambda x: x.definition,
        Instance: lambda x: x.parent,
    }
