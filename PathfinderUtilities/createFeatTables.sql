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

IF OBJECT_ID(N'pf2.Feat', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.Feat
    (
        FeatId INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Feat PRIMARY KEY,
        AonId INT NULL,
        AonUrl NVARCHAR(500) NULL,
        Name NVARCHAR(300) NOT NULL,
        Level INT NULL,
        FeatType NVARCHAR(100) NULL,
        RarityId INT NULL,
        SourceBookId INT NULL,
        SourcePage INT NULL,
        PFS NVARCHAR(100) NULL,
        IsStandardAncestryFeat BIT NOT NULL CONSTRAINT DF_Feat_IsStandardAncestryFeat DEFAULT 0,
        Summary NVARCHAR(MAX) NULL,
        RemasterId NVARCHAR(500) NULL,
        RawHtml NVARCHAR(MAX) NULL,
        RawText NVARCHAR(MAX) NULL,
        RawJson NVARCHAR(MAX) NULL,
        CreatedAt DATETIME2(7) NOT NULL CONSTRAINT DF_Feat_CreatedAt DEFAULT SYSDATETIME(),
        UpdatedAt DATETIME2(7) NOT NULL CONSTRAINT DF_Feat_UpdatedAt DEFAULT SYSDATETIME(),
        LastScraped DATETIME2(7) NOT NULL CONSTRAINT DF_Feat_LastScraped DEFAULT SYSDATETIME(),
        ScrapeVersion NVARCHAR(100) NULL
    );
END
GO

IF OBJECT_ID(N'pf2.FeatSourceLink', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.FeatSourceLink
    (
        FeatSourceLinkId INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_FeatSourceLink PRIMARY KEY,
        FeatId INT NOT NULL,
        SourceBookId INT NOT NULL,
        PageNumber INT NULL
    );
END
GO

IF OBJECT_ID(N'pf2.FeatTrait', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.FeatTrait
    (
        FeatId INT NOT NULL,
        TraitId INT NOT NULL,
        CONSTRAINT PK_FeatTrait PRIMARY KEY (FeatId, TraitId)
    );
END
GO

IF OBJECT_ID(N'pf2.FeatImportLog', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.FeatImportLog
    (
        FeatImportLogId INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_FeatImportLog PRIMARY KEY,
        AonUrl NVARCHAR(500) NULL,
        ImportedAt DATETIME2(7) NOT NULL CONSTRAINT DF_FeatImportLog_ImportedAt DEFAULT SYSDATETIME(),
        Success BIT NOT NULL,
        Message NVARCHAR(4000) NULL
    );
END
GO
