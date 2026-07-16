"""
Version      Date          Author          Descriptions

模块对比主函数
V00.00.0     2025/05/26    zaipeng.qu      Initials Version
V00.00.1     2025/05/27    zaipeng.qu      添加界面，逻辑优化，解决芯片重复
V00.00.2     2025/05/29    zaipeng.qu      取消界面，增加模块复用率处理
V00.00.3     2025/06/30    zaipeng.qu      与模块匹配算法规则合并
V00.00.4     2025/07/10    zaipeng.qu      优化输出逻辑
"""
import spydrnet as sdn
# from moduel_matching import matching_con  # 模块匹配暂未调用
import re


def decode_edf_content(edf_text):
    """进行中文乱码处理"""

    # 定义替换函数处理每个匹配项
    def decode_match(match):
        codes_str = match.group(1) # 提取数字部分，如 "180%%229%%20%%190"
        codes = list(map(int, codes_str.split('%%')))  # 分割并转换为整数列表
        byte_data = bytes(codes) # 转换为字节序列
        try:
            return byte_data.decode('gbk')  # 尝试GBK解码
        except UnicodeDecodeError:
            # 若解码失败，替换无法识别的字符
            return byte_data.decode('gbk', errors='replace')

    # 使用正则匹配所有%数字%序列，并替换为解码后的文本
    decoded_text = re.sub(
        r'%((?:\d+%%)+\d+)%',  # 匹配如%180%%229%的结构
        decode_match,
        edf_text
    )
    return decoded_text


def filter_U_dir(nested_list):
    """过滤不同芯片器件直接相连的网络"""
    # 构建特征字典：{特征元组：U开头首元素的集合}
    feature_dict = {}
    for sub in nested_list:
        if len(sub) >= 5:
            first = sub[0]
            if isinstance(first, str) and first.startswith('U'):
                feature = tuple(sub[2:5]) # 提取第2-5个元素
                if feature not in feature_dict:
                    feature_dict[feature] = set()  # 做去重
                feature_dict[feature].add(first)

    # 过滤符合条件的子列表
    result = []
    for sub in nested_list:
        if len(sub) < 5:
            result.append(sub)
            continue

        first = sub[0]
        feature = tuple(sub[2:5])

        # 判断删除条件
        if isinstance(first, str) and first.startswith('U'):
            if feature in feature_dict and len(feature_dict[feature]) > 1:
                continue  # 跳过需要删除的子列表

        result.append(sub)

    return result


def move_u(lst):
    """
    把U器件挪到前面
    """
    return sorted(lst,
                  key=lambda x: not (isinstance(x, str) and x.startswith('U')))


def euclidean_distance(point1, point2):
    """计算两个点的最小欧几里得距离"""
    return sum((a - b) ** 2 for a, b in zip(point1, point2)) ** 0.5


def merge_sub(nested_list):
    """合并具有公共元素的子列表"""
    # 将每个子列表转换为集合，并逐步合并有交集的集合
    groups = []
    for sublist in nested_list:
        current_set = set(sublist)
        # 查找所有与当前集合有交集的组的索引
        matches = []
        for i, group_set in enumerate(groups):
            if current_set & group_set:  # 检查是否有交集
                matches.append(i)
        # 合并所有匹配的组和当前集合 (需要逆序删除以避免索引错位)
        merged_set = current_set
        for index in reversed(matches):
            merged_set = merged_set.union(groups[index])
            del groups[index]
        groups.append(merged_set)
    # 将集合转换回列表，并保持元素顺序 (可选排序)
    result = [list(group) for group in groups]
    return result


def group_sub(nested_list):
    """
    进行相同连接器件整合
    :param nested_list:
    :return:
    """
    group_dict = {}
    for sublist in nested_list:
        # 将列表元素转换为元组 (如果是列表的话)
        key = (
            tuple(sublist[2]) if isinstance(sublist[2], list) else sublist[2],
            tuple(sublist[3]) if isinstance(sublist[3], list) else sublist[3],
            tuple(sublist[4]) if isinstance(sublist[4], list) else sublist[4],
            tuple(sublist[5]) if isinstance(sublist[5], list) else sublist[5],
        )

        # 分组存储
        if key not in group_dict:
            group_dict[key] = []
        group_dict[key].append(sublist)

    return list(group_dict.values())


def Get_data(NetList):
    """
    获取原理图中的模块数据
    """
    # NetList = sdn.parse(path)
    # 所需数据导出
    # 定义一些列表存储所需数据
    PartRef = []
    PartMPN = []
    PartFootprint = []
    PartValue = []
    PartERP = []
    PartClass = []

    PowerName = []
    pin_ind = []
    pin_net = []
    wire_data = []

    PageNumber = []
    PageName = []
    PageTab = []

    # 通过遍历网表结构获取所需信息
    tab = 0
    for library in NetList.libraries:
        for definition in library.get_definitions():
            if 'INFORMATION' not in definition.name:
                for instance in definition.children:

                    # 处理页码
                    page_name = f'page_name{tab}'
                    page_number = f'page_number{tab}'
                    if instance.name == 'HirainTitleBlock':
                        keyword = 'Page Name'
                        nam = [entry for entry in instance['EDIF.properties'] if
                               entry.get("original_identifier") == keyword]
                        if nam:
                            p_name = nam[0]['value']
                        else:
                            p_name = 'None'
                        if p_name != 'None':
                            ind = p_name.find('_')
                            p_number = p_name[0:ind]
                        else:
                            p_number = 'None'
                        PageTab.append([page_name, p_name, page_number, p_number])
                        tab = tab + 1

                    # 电源数据
                    if instance.reference.name.startswith('VCC') or instance.reference.name.startswith('GND'):
                        VCC_name = instance['.NAME']
                        PowerName.append(VCC_name)

                    # 器件的值获取
                    if 'EDIF.properties.stringDisplay' in instance:
                        part_value = decode_edf_content(instance['EDIF.properties.stringDisplay'])
                        part_value = part_value.replace('37%', '')
                    else:
                        part_value = 'None'

                    # 器件的料号、封装以及等级获取
                    if 'EDIF.properties' in instance:
                        keyword1 = 'PCB Footprint'
                        keyword2 = 'ERP NUM'
                        keyword3 = 'Class'
                        keyword4 = 'Manufacturer Part Number'
                        footprint_str = [entry for entry in instance['EDIF.properties']
                                         if entry.get("original_identifier") == keyword1]
                        erp_str = [entry for entry in instance['EDIF.properties']
                                   if entry.get("original_identifier") == keyword2]
                        class_str = [entry for entry in instance['EDIF.properties']
                                     if entry.get("original_identifier") == keyword3]
                        mpn_str = [entry for entry in instance['EDIF.properties']
                                   if entry.get("original_identifier") == keyword4]
                        if footprint_str:
                            footprint = footprint_str[0]['value']
                        else:
                            footprint = 'None'

                        if erp_str:
                            erp = erp_str[0]['value']
                        else:
                            erp = 'None'

                        if class_str:
                            class_level = class_str[0]['value']
                        else:
                            class_level = 'None'

                        if mpn_str:
                            mpn = mpn_str[0]['value']
                        else:
                            mpn = 'None'
                    else:
                        footprint = 'None'
                        erp = 'None'
                        class_level = 'None'
                        mpn = 'None'

                    # 位号
                    if 'EDIF.designator.stringDisplay' in instance:
                        part_ref = instance['EDIF.designator.stringDisplay']
                        for pin in instance.pins:
                            # pin的坐标数据获取
                            for port in pin.get_ports():
                                if 'EDIF.designator' in port.data:
                                    ind = port.data['EDIF.designator']  # pin序号

                                    # 定位到正确的pin序号
                                    if 'EDIF.portnums' in instance:
                                        pin_count = instance['EDIF.portnums']  # pin数量
                                        for pin_turn in range(1, pin_count + 1):
                                            if (f'EDIF.port{pin_turn}.portInstance.designator.stringDisplay' in instance
                                                    and f'EDIF.port{pin_turn}.portInstance.designator.stringDisplay'
                                                    f'.display.origin.pt' in instance):
                                                if (instance[f'EDIF.port{pin_turn}.portInstance.designator.stringDisplay']
                                                        == ind):
                                                    pin_axis = instance[
                                                        (
                                                            f'EDIF.port{pin_turn}.portInstance.designator'
                                                            f'.stringDisplay.display'
                                                            f'.origin.pt')
                                                    ]
                                                    # 遍历获取网络与cable端点数据
                                                    if pin.wire is not None:
                                                        wire_node = []
                                                        wire_name = pin.wire.cable.name
                                                        figure_count = pin.wire.cable['EDIF.figurenums']
                                                        for figure_tab in range(1, figure_count + 1):
                                                            figure_node = []
                                                            node_count = pin.wire.cable[
                                                                (f'EDIF.figure{figure_tab}.figure.path'
                                                                 f'.pointlist.pointnums')]
                                                            for node_tab in range(1, node_count + 1):
                                                                figure_node.append(pin.wire.cable[
                                                                    f'EDIF.figure{figure_tab}.figure.path'
                                                                    f'.pointlist.point{node_tab}.pt'])
                                                            wire_node.append(figure_node)
                                                        # 对连接在一起的网络cable进行合并
                                                        wire_node = merge_sub(wire_node)

                                                        # 获取正确的cable组合
                                                        correct_wire = 'None'
                                                        if len(wire_node) == 1:
                                                            correct_wire = wire_node[0]
                                                        else:
                                                            for wire in wire_node:
                                                                for node in wire:
                                                                    if euclidean_distance(pin_axis, node) <= 25:
                                                                        correct_wire = wire
                                                        per_wire_data = tuple(correct_wire)
                                                        # if part_ref in ['R3410', 'U3401']:
                                                        #     print('part_ref:', part_ref)
                                                        #     print('wire_name:', wire_name)
                                                        #     print('ind:', ind)
                                                        #     print('pin_axis: ', pin_axis)
                                                        #     print('wire_node:', wire_node)
                                                        #     print('correct_wire:', correct_wire)
                                                        #     print('per_wire_data:', per_wire_data)
                                                        #     print('-------------------------------------------')

                                                        PartRef.append(part_ref)
                                                        PartMPN.append(mpn)
                                                        PartFootprint.append(footprint)
                                                        PartValue.append(part_value)
                                                        PartERP.append(erp)
                                                        PartClass.append(class_level)

                                                        pin_ind.append(ind)
                                                        pin_net.append(wire_name)
                                                        PageNumber.append(page_number)
                                                        PageName.append(page_name)
                                                        wire_data.append(per_wire_data)

    # 所有器件的信息
    df = list(zip(PartRef, pin_ind, pin_net, wire_data, PageName, PageNumber))
    df_all = list(zip(PartRef, PartMPN, PartFootprint, PartValue, PartERP, pin_ind, pin_net, PageName, PageNumber))

    # 页码与页名更新
    for ia in range(0, len(df)):
        df[ia] = list(df[ia])
        df_all[ia] = list(df_all[ia])
        for tabs in PageTab:
            if df[ia][-1] == tabs[2] and df[ia][-2] == tabs[0]:
                df[ia][-1] = tabs[3]
                df[ia][-2] = tabs[1]
            if df_all[ia][-1] == tabs[2] and df_all[ia][-2] == tabs[0]:
                df_all[ia][-1] = tabs[3]
                df_all[ia][-2] = tabs[1]
        df[ia] = tuple(df[ia])
        df_all[ia] = tuple(df_all[ia])

    # 过滤直连的芯片器件
    u_filtered = filter_U_dir(df)
    # 相同连接关系的器件整合
    process = group_sub(u_filtered)
    ref = []
    for a in process:
        ref_line = []
        for a1 in a:
            tar = a1[0]
            if 'U' in tar:
                if '-' in tar:
                    ind = tar.find("-")
                    tar = tar[0:ind]
                else:
                    for i in range(1, len(tar)-1):
                        if tar[i].isdigit():
                            pass
                        else:
                            tar = tar[0:i]
            ref_line.append(tar)
        ref.append(ref_line)

    # 模块整合
    out1 = merge_sub(ref)

    # 把U放到最前面
    out2 = []
    for b in out1:
        b = move_u(b)
        out2.append(b)

    # 只输出以芯片为核心的模块
    model_list = []
    for c in out2:
        if c[0].startswith('U'):
            model_list.append(c)

    # 扩充模块的内容
    out_list = []
    for d in model_list:
        # 记录模块名称
        current_model = []
        # 补全模块参数
        for pf in d:
            for pin_data in df_all:
                tar_pin = pin_data[0]  # 标志说明
                # 位号标志清洗
                if 'U' in pin_data[0]:
                    if '-' in pin_data[0]:
                        ind = pin_data[0].find("-")
                        tar_pin = pin_data[0][0:ind]
                    else:
                        for i in range(1, len(pin_data[0])):
                            if pin_data[0][i].isdigit():
                                pass
                            else:
                                tar_pin = pin_data[0][0:i]
                if pf == tar_pin:
                    current_model.append((pin_data[0:-2]))
        out_list.append(tuple(current_model))

    # 获得模块名称
    model_name = []
    for d in model_list:
        for pin_data in df_all:
            tar_pin = pin_data[0]  # 标志说明
            # 位号标志清洗
            if 'U' in pin_data[0]:
                if '-' in pin_data[0]:
                    ind = pin_data[0].find("-")
                    tar_pin = pin_data[0][0:ind]
                else:
                    for i in range(1, len(pin_data[0])):
                        if pin_data[0][i].isdigit():
                            pass
                        else:
                            tar_pin = pin_data[0][0:i]
            if d[0] == tar_pin:
                model_name.append([pin_data[1], d[0], pin_data[-2]])
                break


    # model_name, 0: 模块名称列表[料号，位号，页名]
    # out_list, 0: 模块器件信息列表，以pin为最小单位
    return model_name, out_list, PowerName


# test_path = r"D:\Private\yuxiang.li\python_development\processing_version\test_data\825504534_GW_SCH_20250714001.EDF"
# test_net = sdn.parse(test_path)
# print(compare(test_net))