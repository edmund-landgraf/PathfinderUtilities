USE [PathfinderUtil];
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'pf2')
BEGIN
    EXEC(N'CREATE SCHEMA pf2');
END
GO

IF OBJECT_ID(N'pf2.Rarity', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.Rarity
    (
        RarityId INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Rarity PRIMARY KEY,
        Name NVARCHAR(100) NOT NULL CONSTRAINT UQ_Rarity_Name UNIQUE
    );
END
GO

IF OBJECT_ID(N'pf2.SourceBook', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.SourceBook
    (
        SourceBookId INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_SourceBook PRIMARY KEY,
        Name NVARCHAR(250) NOT NULL CONSTRAINT UQ_SourceBook_Name UNIQUE
    );
END
GO

IF OBJECT_ID(N'pf2.Trait', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.Trait
    (
        TraitId INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Trait PRIMARY KEY,
        Name NVARCHAR(150) NOT NULL CONSTRAINT UQ_Trait_Name UNIQUE
    );
END
GO

IF OBJECT_ID(N'pf2.Equipment', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.Equipment
    (
        EquipmentId INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Equipment PRIMARY KEY,
        AonId INT NULL,
        AonKey NVARCHAR(100) NULL,
        AonUrl NVARCHAR(500) NULL,
        Name NVARCHAR(300) NOT NULL,
        Level INT NULL,
        EquipmentType NVARCHAR(100) NULL,
        SearchCategory NVARCHAR(100) NULL,
        ItemCategory NVARCHAR(200) NULL,
        ItemSubcategory NVARCHAR(200) NULL,
        RarityId INT NULL,
        SourceBookId INT NULL,
        SourcePage INT NULL,
        PFS NVARCHAR(100) NULL,
        PriceCp INT NULL,
        PriceText NVARCHAR(200) NULL,
        BulkValue DECIMAL(12,4) NULL,
        BulkText NVARCHAR(100) NULL,
        Summary NVARCHAR(MAX) NULL,
        RemasterId NVARCHAR(500) NULL,
        BaseItemText NVARCHAR(MAX) NULL,
        SpellText NVARCHAR(MAX) NULL,
        StageText NVARCHAR(MAX) NULL,
        WeaponCategory NVARCHAR(100) NULL,
        WeaponGroup NVARCHAR(150) NULL,
        WeaponType NVARCHAR(100) NULL,
        Damage NVARCHAR(100) NULL,
        DamageDie INT NULL,
        DamageType NVARCHAR(500) NULL,
        Hands NVARCHAR(50) NULL,
        AmmunitionText NVARCHAR(MAX) NULL,
        ArmorCategory NVARCHAR(100) NULL,
        ArmorGroupText NVARCHAR(MAX) NULL,
        AC INT NULL,
        Hardness INT NULL,
        HardnessText NVARCHAR(100) NULL,
        HP INT NULL,
        HPText NVARCHAR(100) NULL,
        RawHtml NVARCHAR(MAX) NULL,
        RawText NVARCHAR(MAX) NULL,
        RawJson NVARCHAR(MAX) NULL,
        CreatedAt DATETIME2(7) NOT NULL CONSTRAINT DF_Equipment_CreatedAt DEFAULT SYSDATETIME(),
        UpdatedAt DATETIME2(7) NOT NULL CONSTRAINT DF_Equipment_UpdatedAt DEFAULT SYSDATETIME(),
        LastScraped DATETIME2(7) NOT NULL CONSTRAINT DF_Equipment_LastScraped DEFAULT SYSDATETIME(),
        ScrapeVersion NVARCHAR(100) NULL
    );
END
GO

IF OBJECT_ID(N'pf2.EquipmentSourceLink', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.EquipmentSourceLink
    (
        EquipmentSourceLinkId INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_EquipmentSourceLink PRIMARY KEY,
        EquipmentId INT NOT NULL,
        SourceBookId INT NOT NULL,
        PageNumber INT NULL
    );
END
GO

IF OBJECT_ID(N'pf2.EquipmentTrait', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.EquipmentTrait
    (
        EquipmentId INT NOT NULL,
        TraitId INT NOT NULL,
        CONSTRAINT PK_EquipmentTrait PRIMARY KEY (EquipmentId, TraitId)
    );
END
GO

IF OBJECT_ID(N'pf2.EquipmentImportLog', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.EquipmentImportLog
    (
        EquipmentImportLogId INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_EquipmentImportLog PRIMARY KEY,
        AonUrl NVARCHAR(500) NULL,
        ImportedAt DATETIME2(7) NOT NULL CONSTRAINT DF_EquipmentImportLog_ImportedAt DEFAULT SYSDATETIME(),
        Success BIT NOT NULL,
        Message NVARCHAR(4000) NULL
    );
END
GO
