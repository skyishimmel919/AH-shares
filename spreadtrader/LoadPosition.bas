Attribute VB_Name = "LoadPosition"
  Public TimerEnabled3 As Boolean
  
  Sub EnableTimer3() '开始
    TimerEnabled3 = True
    StartTimer3
  End Sub
  
  Sub DisableTimer3() '停用
    TimerEnabled3 = False
  End Sub
  
  Sub StartTimer3()      '注意改代码需要放在模块级
      If TimerEnabled3 = True Then
        Application.OnTime Now + TimeValue("00:05:00"), "StartTimer3" '每5分钟自动运行一次
        LoadPosition '需要每秒运行的代码
      End If
  End Sub
  
 







Sub LoadPosition()


FileCopy "D:\spreadtrader\lib_backup20191115 - IB - 副本\data\position.csv", "D:\spreadtrader\lib_backup20191115 - IB - 副本\data\positioncopy.csv"
FileCopy "D:\spreadtrader\lib_backup20191203 - Tiger - 副本\data\position.csv", "D:\spreadtrader\lib_backup20191203 - Tiger - 副本\data\positioncopy.csv"
FileCopy "D:\spreadtrader\lib_backup20191203 - IB_Sandy\data\position.csv", "D:\spreadtrader\lib_backup20191203 - IB_Sandy\data\positioncopy.csv"

'clear old position

Windows("PositionCheck.xlsm").Activate
Sheets(1).Select
Range("A:AF").Select
Application.CutCopyMode = False
Selection.ClearContents



'Import position from IB_Ken

Workbooks.Open "D:\spreadtrader\lib_backup20191115 - IB - 副本\data\positioncopy.csv"

Windows("positioncopy.csv").Activate
Range("A1:J1").Select
Range(Selection, Selection.End(xlDown)).Select
Selection.Copy
Windows("PositionCheck.xlsm").Activate
Sheets(1).Select
Range("A1").Select
Selection.PasteSpecial Paste:=xlPasteValues, Operation:=xlNone, SkipBlanks _
    :=False, Transpose:=False
 Windows("positioncopy.csv").Activate
 ThisWorkbook.Save
  Windows("positioncopy.csv").Close
  
'Import position from Tiger_Ken
  
Workbooks.Open "D:\spreadtrader\lib_backup20191203 - Tiger - 副本\data\positioncopy.csv"

Windows("positioncopy.csv").Activate
Range("A1:J1").Select
Range(Selection, Selection.End(xlDown)).Select
Selection.Copy
Windows("PositionCheck.xlsm").Activate
Sheets(1).Select
Range("L1").Select
Selection.PasteSpecial Paste:=xlPasteValues, Operation:=xlNone, SkipBlanks _
    :=False, Transpose:=False
 Windows("positioncopy.csv").Activate
 ThisWorkbook.Save
  Windows("positioncopy.csv").Close
  
'Import position from IB_Sandy
  
Workbooks.Open "D:\spreadtrader\lib_backup20191203 - IB_Sandy\data\positioncopy.csv"

Windows("positioncopy.csv").Activate
Range("A1:J1").Select
Range(Selection, Selection.End(xlDown)).Select
Selection.Copy
Windows("PositionCheck.xlsm").Activate
Sheets(1).Select
Range("W1").Select
Selection.PasteSpecial Paste:=xlPasteValues, Operation:=xlNone, SkipBlanks _
    :=False, Transpose:=False
 Windows("positioncopy.csv").Activate
 ThisWorkbook.Save
  Windows("positioncopy.csv").Close
  
  Windows("PositionCheck.xlsm").Activate


End Sub
